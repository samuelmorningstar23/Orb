# Dual Experience: Visual Design Specification

**Scope:** the doctor home (`Today`) and the nurse home (`My Shift`).
**Status:** buildable. Every value below is final; where a judgement was made, the
rejected alternative and the reason are stated inline so the next person inherits
the reasoning, not just the result.

**This document decides how the two homes LOOK and FEEL.** It does not decide what
they contain, the information architecture is fixed by
`docs/DUAL_EXPERIENCE_DESIGN.md` §2 and is not re-opened here. It does not decide
data plumbing; every block composes reads that already exist.

**Authority:** `DESIGN.md` ("Quiet Clinical") is law. `src/index.css` is the only
source of colour, spacing, radii, type and motion values. This spec adds exactly
one new token (§1.6) and no new colours.

---

## 0. The five rules that fail the build gate

Restated at the top because they are the ones most often broken at 2am.

1. **Never express de-emphasis or state with `opacity`.** It multiplies down the
   tree and dims label and background by the same factor, so contrast can only get
   worse. Use `--text-muted` / `--text-secondary` / `--btn-disabled-{bg,text,border}`.
2. **Two token families per status.** `--status-x` for fills and borders,
   `--status-x-text` for small text. Text sitting on a solid status fill uses
   `--status-on-fill`. Status chip background tint **caps at 20%**; these homes use
   **12%** (§1.7).
3. **Tap targets: 24px floor everywhere, 44px under `@media (pointer: coarse)`.**
   That one rule is the entire tablet story. Do not add a "tablet mode".
4. **No hover-only affordances.** A label, control or meaning that appears only on
   hover does not exist on a bedside tablet. `title` is not a label.
5. **Every text/surface pair passes 4.5:1 in dark, light and night.**
   `node scripts/check-contrast.mjs` is the gate. §10 lists every pair these homes
   rely on, with measured numbers, so it can be checked without guessing.

---

## 1. Foundations

### 1.1 Typographic hierarchy

One ramp, used by both homes. `token` is the `src/index.css` variable; `px` is at
the root 16px. Line-height is **always declared**, the global `body { line-height:
1.6 }` is prose rhythm and is wrong for dense rows.

| # | Element | Token | px | Weight | LH | Tracking | Colour |
|---|---|---|---|---|---|---|---|
| T1 | Home title (`h1`) | `--text-xl` | 26.4 | 560 | 1.15 | -0.02em | `--text-primary` |
| T2 | Home context line | `--text-sm` | 14 | 450 | 1.40 | 0 | `--text-muted` |
| T3 | Section header label | `--text-xs` | 12.5 | 600 | 1.20 | 0.07em, UPPERCASE | `--text-muted` |
| T4 | Count chip number | `--text-sm` | 14 | 650 | 1.00 | 0 | see §1.7 |
| T5 | Row primary (name / task) | `--text-base` | 16 | 600 | 1.30 | -0.006em | `--text-primary` |
| T6 | Row secondary (detail) | `--text-sm` | 14 | 450 | 1.40 | 0 | `--text-secondary` |
| T7 | Row meta (bed · MRN · age) | `--text-xs` | 12.5 | 500 | 1.35 | 0 | `--text-muted` |
| T8 | Row trailing value (lateness, count) | `--text-sm` | 14 | 650 | 1.20 | 0 | `--text-secondary` → status |
| T9 | Micro-caps tag (OBS / MED / SIGN) | 0.7rem | 11.2 | 700 | 1.00 | 0.06em, UPPERCASE | `--status-on-fill` |
| T10 | NEWS2 value, dense badge | `--text-lg` | 20.8 | 700 | 1.10 | 0 | per band, §5.8 |
| T11 | Totals-strip number | `--text-md` | 18 | 700 | 1.10 | 0 | `--text-primary` → status |
| T12 | Empty/error state title | `--text-md` | 18 | 650 | 1.30 | -0.01em | `--text-primary` |
| T13 | Empty/error state body | `--text-sm` | 14 | 450 | 1.50 | 0 | `--text-secondary` |
| T14 | Footnote / reassurance | `--text-xs` | 12.5 | 450 | 1.50 | 0 | `--text-muted` |

**Size floors.** Three tiers, and they are not the same rule:
- **Prose** (anything a clinician reads as a sentence): never below `--text-sm`
  (14px), at any width, on any pointer.
- **Supporting meta** (T7, T14): floor `--text-xs` (12.5px).
- **Micro-caps tags** (T9): floor **0.7rem (11.2px)**, and only permitted for a
  category label that duplicates information already present in an adjacent word.
  A tag is never the only carrier of meaning.

> **Change to the existing nurse home:** `.task-row__kind`, `.news2__label` and
> `.med-group__count` are at `0.62rem` (9.9px) today. Raise every one to `0.7rem`.
> Nothing else about them changes.

**`--text-2xl` (35.2px) is not used on either home.** The largest thing on a home is
T1. Rejected alternative: a hero "overdue" number at `--text-2xl`. Rejected because
on a true-black night canvas a 35px `--text-primary` (`#ffffff`, 19:1) block is a
glare source at 3am, and because a number that large implies a dashboard; these are
work lists.

### 1.2 When Fraunces is permitted

**Exactly one element per home may be Fraunces: the home's `<h1>` (T1).**

Everything else, section headers, row titles, numbers, empty-state titles, chips,
buttons, footnotes, is Inter (`--font-family`). Fraunces is a brand voice, not a UI
face; a serif in a dense clinical list is slower to read and reads as editorial.

The shell header's `.shell__page-title` is also Fraunces. The two coexist because
they are separated by the header hairline and differ by 11px, which reads as
*chrome, then page* rather than as two headings.

Declaration for T1 (do not use the `.font-display` class, it sets `opsz` 28 with
weight 540; the homes want the shell's tuning at a larger size):

```
font-family: var(--font-display);
font-variation-settings: 'SOFT' 0, 'WONK' 0, 'opsz' 28;
font-size: var(--text-xl); font-weight: 560;
letter-spacing: -0.02em; line-height: 1.15;
```

- *Rejected:* dropping Fraunces from the home `h1` to avoid doubling with the shell
  title. Rejected because the home title is the single hero moment either experience
  gets, and the shell title is a functional breadcrumb ("Orb Today") the eye skips.
- *Rejected:* a time-of-day greeting ("Good evening, Dr Sharma"). Rejected because it
  is decoration, it is wrong for a night doctor at 03:00, and it displaces the ward
  name, which is the one piece of orientation that matters when you are covering
  someone else's list.

### 1.3 Numerals

Global rules already set `font-variant-numeric: tabular-nums lining-nums` on
`.tabular-nums`, `[class*="stat|metric|kpi|score"]`, `td/th/input`, `.mono`. The
homes' classes match none of those patterns, so **every numeric element on both
homes declares it explicitly.** A count that jitters 1px wider when it ticks from 9
to 10 is how a calm screen becomes a twitchy one.

Which face:
- **`--font-mono` (JetBrains Mono)**, values compared *character by character*:
  MRN, bed identifiers, clock times (`03:14`), dose strings (`4.5 g`), order IDs.
- **Inter + tabular-nums**, values compared *by magnitude*: counts, NEWS2, minutes
  late, "7 of 14". Mono at 12–14px is narrower and slower to scan for size.

Applies to: T4, T8, T10, T11, and any inline count.

### 1.4 Spacing

`--space-1` 4 · `--space-2` 8 · `--space-3` 12 · `--space-4` 16 · `--space-5` 24 ·
`--space-6` 32 · `--space-7` 40 · `--space-8` 48. **No raw pixel spacing anywhere on
either home**, with one exception: the shared row `min-height` values in §5.4, which
are hit-target geometry, not rhythm.

| Relationship | Desktop | ≤768px |
|---|---|---|
| Home title → context line | `--space-1` | `--space-1` |
| Header block → first section | `--space-6` | `--space-5` |
| Section → section | `--space-6` | `--space-5` |
| Section header → its list | `--space-3` | `--space-3` |
| Row → row, card list (`--cards`) | `--space-2` | `--space-2` |
| Row → row, divided list (`--rows`) | 0 (1px hairline) | 0 |
| Row internal padding | `--space-3` block / `--space-4` inline | `--space-4` / `--space-4` |
| Row lead → body | `--space-3` | `--space-3` |
| Row body → trailing | `--space-4` | `--space-3` |
| Inside a text stack (primary→secondary→meta) | `--space-1` | `--space-1` |
| Card padding (patient card, state block) | `--space-4` | `--space-4` |
| State block (empty/error) block padding | `--space-6` | `--space-5` |
| Chip padding | `2px --space-2` | `2px --space-2` |
| Two-column grid gap | `--space-5` | n/a |

The page gutter is **not** the home's business, `.shell__body` owns it
(`ClinicianShell.css:537`, with the incident that produced the rule recorded above
it). Neither home stylesheet may declare page padding.

### 1.5 Breakpoints and grid

Four steps, chosen to match the shell's existing rail transitions so a rotating
tablet changes shape **once**, not twice.

| Step | Range | Shell rail | Doctor Today | Nurse My Shift |
|---|---|---|---|---|
| Desktop | ≥1400px | full 232px | 2 col: `minmax(0,1fr) 360px` | 1 col, `max-width: var(--home-col-max)` |
| Desktop narrow | 1200–1399px | full 232px | 2 col: `minmax(0,1fr) 320px` | as above |
| Tablet landscape | 769–1199px | icon rail at ≤1024 | **1 col**, `max-width: var(--home-col-max)` | as above |
| Tablet portrait | 561–768px | horizontal top bar | 1 col, full width | 1 col, full width |
| Phone | ≤560px | top bar | 1 col, full width | 1 col, full width |

Media queries to write (desktop-first, matching house style):
`@media (max-width: 1399px)` · `@media (max-width: 1199px)` ·
`@media (max-width: 768px)` · `@media (max-width: 560px)`.

**Why the aside collapses at 1199 and not at the shell's 1024:** below ~1200px the
main column falls under 560px once the 232px rail, the 56px gutters and a 320px
aside are subtracted, and a two-line task row at 540px starts wrapping its trailing
value. The aside stops earning its place before the rail collapses.
- *Rejected:* collapsing at 1024 to reuse the shell's breakpoint exactly. Rejected
  because iPad landscape (1024–1180 CSS px) is a primary device and it would ship a
  measurably cramped two-column layout there.

**Density differs by device, not by role.** Both homes use identical row heights,
paddings and type at any given width. There is no "nurse spacing" and no "doctor
spacing"; the only thing that changes with the pointer is the tap target (§0.3) and
the row padding that follows from it.

**The nurse queue is single-column at every width.** Rejected alternative: a
two-column task grid at ≥1400px to reduce scrolling. Rejected because the list is
*sorted*, deterioration, then lateness, and two columns destroy the reading order
that is the entire product. "What do I do next" must be answerable by looking at the
top.

### 1.6 New token (one)

```css
:root {
  /* Reading measure for a home column. Both homes and (later) the nurse patient
     sheet clamp to it, so a 2560px monitor does not stretch a task row until the
     lateness value is 1,400px from the patient's name. Theme-independent, like
     --tap-min, so it is declared once. No contrast implication: it is a length. */
  --home-col-max: 960px;
}
```

Value chosen from the row: a 56px NEWS2 lead + `--space-3` + a body that holds a
full drug name, dose, route and frequency on one line at `--text-sm` (~640px) +
`--space-4` + a 120px trailing column + 2×`--space-4` padding ≈ 940px. 960 is the
next value on the 4px grid with headroom.

No other token is added. **No new colour is introduced anywhere in this spec.**

### 1.7 Status vocabulary (the anti-cry-wolf rules)

These are the rules that stop the homes becoming an alarm panel. They are checkable.

| Treatment | Value | Reserved for |
|---|---|---|
| **Solid status fill** | `background: var(--status-danger \| --status-warn)`, label `--status-on-fill` | The NEWS2 badge at `critical` / `elevated`. **Nothing else on either home, ever.** |
| **Status left rule** | `border-left: 4px solid var(--status-danger)` (overdue) / `3px solid var(--status-warn)` (due soon) | A row that is late or deteriorating |
| **Status-tinted chip** | `color-mix(in srgb, var(--status-x) 12%, transparent)` fill, `color-mix(… 40%, transparent)` border, `--status-x-text` label | A non-zero adverse count |
| **Status text** | `--status-x-text` | A trailing value that is itself the bad news ("42 min late") |

And:

- **A zero adverse count renders neutral, never green.** `--bg-glass` fill,
  `--border-subtle`, `--text-secondary`. Green (`--status-ok`) appears on these homes
  in exactly two places: the "nothing due" empty state icon (§6.2) and a handover
  item already given. Green is *"actively achieved"*, not *"not currently bad"*.
- **The section header label never takes a status colour.** The label is constant;
  the *count chip* carries the tint. "OVERDUE" in danger red every single shift is
  how a colour stops meaning anything.
  - *Rejected:* the current `.nurse-section__title--danger` treatment (the whole
    header in `--status-danger-text`). Rejected for exactly that reason, it is red
    on a quiet shift and red on a bad one.
- **One loud element per section maximum.** Four overdue rows produce four left
  rules and one tinted chip, not four fills.
- **Never use a dashed border for status.** Dashed already means *model-estimated*
  in this system (`DESIGN.md` §5) and *empty* in the state blocks.
- **The accent (`--accent-*`) is never status.** On each home the clay/amber accent
  appears in exactly three places: the focus ring, the one primary button, and the
  nurse's active view-tab underline. Nowhere else, no accent washes, no accent
  chips, no accent rules. This matters most in **night**, where the accent
  (`#ffb454`) and `--status-warn` (`#ffc043`) are ~11 hue-degrees apart and an
  accent-washed chip beside a warn chip is genuinely ambiguous at 3am.

---

## 2. The two homes as one family

### Deliberately identical

- Home header: T1 title, T2 context line, one ghost Refresh control, same geometry.
- Section header: micro-caps label (T3) + count chip (T4), same everywhere.
- **Row chassis**, one component, `.orb-row` (§5.4): the same min-heights, the same
  lead/body/trailing structure, the same hover, the same focus, the same left-rule
  vocabulary.
- Two list rhythms, `--cards` and `--rows` (§5.5), chosen by what the row *is*, never
  by which home it is on.
- All three state blocks (loading / empty / error), the skeleton, and the rule that
  an empty state must name what it searched and when.
- Status vocabulary (§1.7), NEWS2 badge, offline banner, focus treatment, motion,
  spacing scale, tap targets, type ramp.

### Deliberately different

| | Doctor "Today" | Nurse "My Shift" | Why |
|---|---|---|---|
| Structure | **One scroll**, no view tabs | **Four view tabs** | The doctor's four questions are answered *simultaneously* in the first minute; the nurse's four jobs are *sequential and mutually exclusive* across a shift. Handover is not something you glance at while doing meds. |
| Columns | 2 at ≥1200px | 1 always | A triage is parallel; a queue is ordered. |
| Verb | **Go.** Rows navigate; the whole row is one target ending in `›` | **Record.** Rows open a write sheet; patient cards carry explicit action buttons | The sharpest family distinction, and it is honest: nothing on the doctor home writes. |
| Summary strip | none | 5-chip totals strip | House-wide numbers belong to Command Center. The doctor home is a to-do, not a dashboard. |
| Accent button | Sage send | Record obs (per patient card) | One each. |

The rule that makes this a family rather than two products: **a row looks the same
on both homes; what it does when you press it is what differs.**

---

## 3. Doctor home: "Today"

### 3.1 Wireframe: desktop (≥1400px)

The frame (rail, header) is the shell's. This is `.shell__body` content only.

```
┌────────────────────────────────────────────────────────────────────────────────┐
│ Today                                                              [ Refresh ] │ T1 + ghost
│ General Medicine · Tue 12 Aug · updated 09:14                                  │ T2
│                                                                                │ ← --space-6
├─────────────────────────────────────────────────┬──────────────────────────────┤
│ DETERIORATING                               (3) │ NEW RESULTS              (7) │ T3 + chip
│ ┌─────────────────────────────────────────────┐ │ ┌──────────────────────────┐ │
│ │▌│ 9 │ GM-04 · Anita Rao        2 new     ›  │ │ │ GM-04 Anita Rao    3   › │ │
│ │ │   │ Sepsis · RR 26 · SpO₂ 91%             │ │ ├──────────────────────────┤ │
│ └─────────────────────────────────────────────┘ │ │ GM-11 Vikas Shah   2   › │ │
│ ┌─────────────────────────────────────────────┐ │ ├──────────────────────────┤ │
│ │▌│ 7 │ GM-11 · Vikas Shah                 ›  │ │ │ GM-02 Sara Iqbal   2   › │ │
│ │ │   │ Pneumonia · HR 118                    │ │ └──────────────────────────┘ │
│ └─────────────────────────────────────────────┘ │      list--rows (divided)    │
│ ┌─────────────────────────────────────────────┐ │                              │
│ │▌│ 6 │ GM-02 · Sara Iqbal                 ›  │ │ END OF SHIFT                 │
│ │ │   │ CCF · new O₂ requirement              │ │ ┌──────────────────────────┐ │
│ └─────────────────────────────────────────────┘ │ │ Confirm last night's     │ │
│              list--cards (gap 8)                │ │ shift, 1 owed        ›  │ │
│                                                 │ └──────────────────────────┘ │
│ PENDING MY DECISION                         (5) │   (only if outcome.record)   │
│ ┌─────────────────────────────────────────────┐ │                              │
│ │▌│ SIGN  3 notes awaiting your signature   › │ │                              │
│ ├─────────────────────────────────────────────┤ │                              │
│ │▌│ ACK   1 safety alert unresolved         › │ │                              │
│ ├─────────────────────────────────────────────┤ │                              │
│ │ │ ROUND 7 of 14 patients seen             › │ │                              │
│ │ │       Started 09:02 · 7 remaining         │ │                              │
│ └─────────────────────────────────────────────┘ │                              │
│                                                 │                              │
│ ┌─────────────────────────────────────────────┐ │                              │
│ │ Ask Sage about this ward…             [ ➤ ] │ │                              │
│ └─────────────────────────────────────────────┘ │                              │
└─────────────────────────────────────────────────┴──────────────────────────────┘
   main: minmax(0, 1fr)                    gap 24         aside: 360px (320 <1400)
```

### 3.2 Wireframe: tablet portrait (768px)

One column. Source order is the visual order; no `order:` reshuffling, so the DOM
and the screen agree for keyboard and screen-reader users.

```
┌──────────────────────────────────────────────────────────────┐
│ Today                                            [ Refresh ] │
│ General Medicine · Tue 12 Aug · updated 09:14                │
│                                                              │
│ DETERIORATING                                            (3) │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │▌│ 9 │ GM-04 · Anita Rao              2 new           ›   │ │  ≥44px targets;
│ │ │   │ Sepsis · RR 26 · SpO₂ 91%                          │ │  row ≈ 76px
│ └──────────────────────────────────────────────────────────┘ │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │▌│ 7 │ GM-11 · Vikas Shah                             ›   │ │
│ │ │   │ Pneumonia · HR 118                                 │ │
│ └──────────────────────────────────────────────────────────┘ │
│                                                              │
│ PENDING MY DECISION                                      (5) │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │▌│ SIGN  3 notes awaiting your signature              ›   │ │
│ ├──────────────────────────────────────────────────────────┤ │
│ │▌│ ACK   1 safety alert unresolved                    ›   │ │
│ ├──────────────────────────────────────────────────────────┤ │
│ │ │ ROUND 7 of 14 patients seen                        ›   │ │
│ └──────────────────────────────────────────────────────────┘ │
│                                                              │
│ NEW RESULTS                                              (7) │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │ GM-04 Anita Rao                              3         › │ │
│ ├──────────────────────────────────────────────────────────┤ │
│ │ GM-11 Vikas Shah                             2         › │ │
│ └──────────────────────────────────────────────────────────┘ │
│                                                              │
│ END OF SHIFT   (only if the account holds outcome.record)    │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │ Confirm last night's shift, 1 owed                    › │ │
│ └──────────────────────────────────────────────────────────┘ │
│                                                              │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │ Ask Sage about this ward…                        [  ➤  ] │ │
│ └──────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

### 3.3 Hierarchy of attention

| Rank | What | Mechanism that guarantees it |
|---|---|---|
| **1st** | The most deteriorated patient | **Position** (first block, top of the main column = first fixation in LTR) + **the only solid status fills above the fold** (the NEWS2 badges) + **the tallest rows** (two-line, `--cards` rhythm with 8px separation, so each is its own object). |
| **2nd** | What is waiting on this doctor's signature or acknowledgement | **Position** (immediately below, same column, same reading line) + a **status-tinted count chip**, outline-tinted, not filled, so it reads as second. |
| **3rd** | New results | **Position** (aside column, or third in one column) + **neutral treatment only**: `--rows` rhythm, no left rules, no status colour, counts in `--text-secondary`. It is a fact list, not an alarm. |
| tool | Ask Sage | Last, lowest, surface-coloured, no accent until focus. You reach for it after reading, never before. |

**Why deteriorating patients win without shouting.** The escalation is *structural*,
not chromatic: the eye lands on the top-left block because it is first and because
its rows are the only two-line, individually-bordered objects on the page. Colour
does one job on top of that, a single 4px rule and a filled NEWS2 badge on the
patients that earned it. If nobody is deteriorating, that section renders its empty
state and the page has **no solid status colour at all**. That is the test: on a
quiet ward, Today is a warm grey page with three headings. On a bad ward, three red
badges are the only saturated pixels on screen. That contrast is what makes them
mean something.

### 3.4 Block specifications

**Header.** `.orb-home__header` (shared). Left: T1 + T2. Right: `.orb-home__refresh`
(ghost, 36px / 44px coarse, label "Refresh" → "Refreshing…" while in flight,
`aria-busy="true"`, disabled state uses the neutral pair, never opacity). The
Refresh control is never the only path to fresh data, the 60s poll continues.

**Deteriorating.** `.orb-list--cards`. Rows sorted by the existing Vigil stability
sort; **top 5 only**, with a trailing `.orb-row--more` link "See all 12 patients ›"
that fires `gotoTab('vigil')`. Rejected: showing every patient. Rejected because a
composed home that reproduces the roster is just the roster with extra steps.
- Lead: `News2Badge` `size="sm" labelled={false}`.
- Primary (T5): `{bed} · {name}`, bed first, because the doctor is looking for a
  room, not a name.
- Secondary (T6): diagnosis + up to two abnormal vitals, joined ` · `. Truncate with
  ellipsis, never wrap to a third line.
- Trailing (T8): unseen-chart count as `{n} new` when > 0, else empty. Then `›`.
- Left rule: 4px `--status-danger` at `critical`, 3px `--status-warn` at `elevated`,
  `--border-subtle` otherwise.
- Press: `openPatient(id)`, the existing bus call, which does `gotoTab('vigil')`
  then dispatches `orb:open-patient`.

**Pending my decision.** `.orb-list--rows` (one bordered card, hairline-divided).
Three row kinds, always in this order, each omitted when its count is zero:
- `SIGN` (`--status-info` tag) → `gotoTab('scribe')`
- `ACK` (`--status-danger` tag) → `gotoTab('vigil')`
- `ROUND` (no tag; see below) → `gotoTab('round')`

The round row's primary is `7 of 14 patients seen`, secondary `Started 09:02 · 7
remaining`, trailing `Resume ›`. **No progress bar.**
- *Rejected:* the `Round.css` `.round-rail__bar` progress bar. Rejected on three
  counts: it duplicates the number sitting next to it; it would be the only animated
  geometry on the page (`transition: width`); and its fill is `--accent-gold`, which
  would be a fourth accent use and put clay next to a status chip. If a bar is ever
  wanted, it goes in as a static fill with no transition.

**New results.** `.orb-list--rows`, aside column. Primary `{bed} {name}` (T5),
trailing the count (T8, `--text-secondary`, tabular) + `›`. **No status colour**, no
rules. Aggregate header count = sum. Press → `openPatient(id)`.

**End of shift.** Renders **only** when the account holds `outcome.record`
(`hasPermission(OUTCOME_RECORD, user?.role)`). Single `.orb-row` → opens
`OutcomeCaptureSheet` in the existing portal sheet. It shows **owed-shift state
only**, never a rate, never a count of events, never a scorecard number. The
blinding boundary in `permissions.py:83-89` applies to this block verbatim.

**Ask Sage.** `.today-ask`, see §5.10. Its stylesheet ownership is load-bearing;
read §5.10 before writing it.

---

## 4. Nurse home: "My Shift"

Most of this exists. This section is written as a **diff**: what changes, and, more
importantly, what must not.

### 4.1 Wireframe: desktop

```
┌────────────────────────────────────────────────────────────────────────────────┐
│ My shift                                                           [ Refresh ] │
│ General Medicine · Tue 12 Aug · updated 09:14                                  │
│                                                                                │
│ 12 patients   (1 critical) (3 elevated) (2 obs overdue) (1 dose overdue)       │ totals
│                                                                                │
│ ┌ Due now (4) ─┬ My patients (12) ─┬ Handover ─┬ End of shift ─────────────────┐│ tablist
│ └──────────────┴───────────────────┴───────────┴───────────────────────────────┘│
│                                                                                │
│ OVERDUE                                                                    (2) │
│ ┌────────────────────────────────────────────────────────────────────────────┐ │
│ │▌│ 9 │ GM-04 · Anita Rao                                    42 min late     │ │
│ │ │   │ OBS  Observations · critical · minimum 30-minutely                    │ │
│ └────────────────────────────────────────────────────────────────────────────┘ │
│ ┌────────────────────────────────────────────────────────────────────────────┐ │
│ │▌│ 5 │ GM-11 · Vikas Shah                                   12 min late     │ │
│ │ │   │ MED  Piperacillin-tazobactam · 4.5 g · IV · 8-hourly   1 not charted  │ │
│ └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                │
│ DUE WITHIN 30 MINUTES                                                      (2) │
│ ┌────────────────────────────────────────────────────────────────────────────┐ │
│ │▌│ 3 │ GM-07 · Deepa Menon                                  in 18 min       │ │
│ │ │   │ OBS  Observations · stable · minimum 4-hourly                         │ │
│ └────────────────────────────────────────────────────────────────────────────┘ │
│                                                                                │
│ Observation intervals follow the RCP NEWS2 escalation table. Medication due     │
│ times are derived from each order's frequency and the last charted dose.        │
└──────────────────────────── max-width: var(--home-col-max) ────────────────────┘
```

### 4.2 Wireframe: tablet portrait (768px)

```
┌──────────────────────────────────────────────────────────────┐
│ My shift                                         [ Refresh ] │
│ General Medicine · Tue 12 Aug · updated 09:14                │
│                                                              │
│ 12 patients                                                  │
│ (1 critical) (3 elevated) (2 obs overdue) (1 dose overdue)   │  wraps, order fixed
│                                                              │
│ ┌ Due now (4) ─┬ My patients (12) ─┬ Handover ─┬ End of… ──┐ │  h-scroll, no fade
│ └──────────────┴───────────────────┴───────────┴───────────┘ │
│                                                              │
│ OVERDUE                                                  (2) │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │▌│ 9 │ GM-04 · Anita Rao                                  │ │  trailing value
│ │ │   │ OBS  Observations · critical · min 30-minutely      │ │  drops below the
│ │ │   │ 42 min late                                        │ │  body at ≤768
│ └──────────────────────────────────────────────────────────┘ │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │▌│ 5 │ GM-11 · Vikas Shah                                 │ │
│ │ │   │ MED  Piperacillin-tazobactam · 4.5 g · IV           │ │
│ │ │   │ 12 min late · 1 not charted                        │ │
│ └──────────────────────────────────────────────────────────┘ │
│                                                              │
│ DUE WITHIN 30 MINUTES                                    (2) │
│  …                                                           │
└──────────────────────────────────────────────────────────────┘
```

### 4.3 Hierarchy of attention

| Rank | What | Mechanism |
|---|---|---|
| **1st** | The overdue list | **Size and object weight.** Rows are 76px bordered surfaces with a 4px danger rule and a filled NEWS2 badge; the strip above them is 32px chips. Size beats position, and both sit inside the first 240px so the eye takes them as one unit. |
| **2nd** | The non-zero adverse chips in the totals strip | 12% tint + `--status-x-text` number. Zero chips stay neutral and recede. |
| **3rd** | Due within 30 minutes | Same rows, 3px warn rule, warn trailing text. |

**The strip answers "how is my ward?", read once at handover. The list answers
"what do I do next?", read every ten minutes.** That is why the summary is
deliberately the quieter of the two despite sitting on top.

**No row is ever marked as "next".** Rejected alternative: accenting the first
overdue row as the suggested next action. Rejected because nurse work is
interruption-driven and proximity-driven, the next task is whichever bay she is
standing in, and declaring one would be wrong most of the time and would spend the
accent on a guess.

### 4.4 What changes

1. **Header.** T1 gains the Fraunces declaration in §1.2 (it already uses
   `--font-display` at weight 600; move to 560 + the variation settings). T2 gains
   the weekday and date: `{department} · {ddd d MMM} · updated {HH:MM}` with the
   clock time in `--font-mono`. Refresh becomes the shared `.orb-home__refresh`.
2. **Totals strip** (`.nurse-totals`). Fixed order, never re-sorted. `12 patients`
   becomes a plain T2 lead-in, not a pill. The four adverse counters become shared
   `.orb-count-chip`s: neutral at zero, 12%-tinted at non-zero (they are 12% today, 
   keep), number at T11, label at T7. **Remove `role="status"` from the container**
   (§9.5).
3. **Section headers.** `.nurse-section__title` and `.nurse-section__count` are
   deleted and replaced by the shared `.orb-section-header` + `.orb-count-chip`.
   `--danger` header colouring goes away (§1.7).
4. **Task rows.** `.task-row` adopts the shared geometry and type (§5.4): 56px
   desktop / `--tap-min` floor with `--space-4` block padding on coarse pointer;
   T5/T6/T7/T8; explicit `tabular-nums`; overdue rule 3px → **4px**; `.task-row__kind`
   0.62rem → 0.7rem.
5. **Row hover is a live contrast defect and must change.** `.task-row:hover`
   currently sets `background: var(--bg-glass-hover)`, under which
   `.task-row__patient` and `.task-row__detail` (`--text-muted`) measure **4.17:1 in
   dark**, below AA. Change to `background: var(--bg-glass)` (4.78:1) +
   `border-color: var(--border-glass)`. Same fix for `.handover__toggle:hover`, whose
   `.handover__meta` is `--text-muted`. See §10.2, the palette gate cannot see this.
6. **Empty and loading states.** `.nurse-empty` (a dashed grey box used for "nothing
   overdue", "no patients in scope" *and* effectively for loading) is deleted. All
   three become the shared, distinct state blocks in §6. The nurse "nothing due"
   state is designed in §6.2 and is the most important state on either home.
7. **List width.** The task list and the handover list clamp to
   `var(--home-col-max)`, left-aligned. The patient grid does **not** clamp, it is a
   `auto-fill minmax(300px, 1fr)` grid and should use the width it is given.
8. **`.news2*` rules move out of `NursePanel.css`** into `ClinicianShell.css`, and
   `News2Badge.tsx` moves to `src/components/News2Badge.tsx`. Both homes render it;
   see §5.1.
9. **`News2Badge` gains an `aria-label`** carrying the same sentence its `title`
   carries today (§9.4). The `title` stays for pointer users.

### 4.5 What must NOT change

Touching any of these is out of scope tonight and each has a reason recorded in the
code:

- The task-building and sort logic in `NursePanel.tsx:58-104`, deterioration first,
  then lateness; PRN/continuous/unscheduled excluded. This is clinical behaviour.
- `useOfflineSnapshot` and the fact that `board` is the **snapshot**, not the raw
  state (`NursePanel.tsx:137-141`), so a cached copy can never render unbannered.
- The 60s poll being **paused while a sheet is open** (`:146-150`).
- End-of-shift capture **not** being gated on `board` (`:382-390`), a broken board
  must not stop the pilot's evidence instrument.
- The roving-tabindex tablist (`:248-274`).
- The absence of a department selector (`:207-210`), scope is server-derived.
- Every sheet: `ObsEntrySheet`, `MedRoundSheet`, `HandoverSheet`,
  `OutcomeCaptureSheet`, and all of `.obs-*`, `.med-*`, `.handover-*`,
  `.shift-*`, `.nurse-sheet*`, `.nurse-confirm*`, `.nurse-btn*`, `.nurse-note*`.
  **None of them is in scope.** Do not rename them for consistency; a 1,200-line
  rename at 2am is how a green suite turns red.
- The four view tabs, their labels, and their order.

---

## 5. Component inventory

**Stylesheet ownership is a correctness question, not a style question.** Panel
stylesheets ship inside that panel's lazy chunk. A shared class styled inside a lazy
panel's stylesheet **renders unstyled until that panel is visited**, this repo has
already shipped that bug, and the comment at `NursePanel.css:1-16` and
`ClinicianShell.css:623-626` both exist because of it.

**Rule: if a class is used by both homes, it lives in `ClinicianShell.css`.**

| # | Component | Class | Stylesheet | New / existing |
|---|---|---|---|---|
| 5.1 | NEWS2 badge | `.news2*` | **`ClinicianShell.css`** | **moved** from `NursePanel.css` |
| 5.2 | Home header | `.orb-home__header`, `__title`, `__context`, `__refresh` | `ClinicianShell.css` | new |
| 5.3 | Section header + count chip | `.orb-section-header` (exists), `.orb-section-header__label`, `.orb-count-chip` | `ClinicianShell.css` | extend + new |
| 5.4 | Row chassis | `.orb-row` + parts | `ClinicianShell.css` | new |
| 5.5 | List rhythms | `.orb-list--cards`, `.orb-list--rows` | `ClinicianShell.css` | new |
| 5.6 | Micro-caps tag | `.orb-tag`, `--obs/--med/--sign/--ack` | `ClinicianShell.css` | new |
| 5.7 | State blocks | `.orb-state--loading/--empty/--error` | `ClinicianShell.css` | new (ported from `RoundStates`) |
| 5.8 | Skeleton | `.orb-skeleton`, `__bar`, `@keyframes orb-skeleton-pulse` | `ClinicianShell.css` | new |
| 5.9 | Patient card | `.patient-card*` | `NursePanel.css` | existing, restyled |
| 5.10 | Inline Sage composer | `.today-ask*` | `DoctorHome.css` | new, **must not reuse `.sage-composer`** |
| 5.11 | Round resume row | `.today-resume` (text only) | `DoctorHome.css` | new |
| 5.12 | Totals strip | `.nurse-totals`, `.nurse-total` | `NursePanel.css` | existing, restyled |
| 5.13 | View tab strip | `.nurse-views`, `.nurse-view-tab` | `NursePanel.css` | existing, unchanged |
| 5.14 | Offline banner | `OfflineBanner` | own file (eager) | existing, unchanged |

### 5.1 NEWS2 badge: the move, and why

Both homes render it. Its rules are currently in the nurse's lazy chunk, so a doctor
who opens Today before ever visiting the nurse panel, which is every doctor, since
they have no `shift` tab, would see an **unstyled** NEWS2 badge: a bare number with
no box, no colour and no band. That is not a cosmetic bug; the band *is* the signal.

Move, verbatim, from `NursePanel.css:267-339` to `ClinicianShell.css`:
`.news2`, `.news2__label`, `.news2__value`, `.news2--lg`, `.news2--critical`,
`.news2--elevated`, `.news2--stable`, `.news2--none`, `.news2__flag`.
Move `News2Badge.tsx` → `src/components/News2Badge.tsx` and update the three
importers. No visual change.

Geometry (unchanged except T9/T10): `min-width: 52px`, `--radius-md`,
`padding: var(--space-1) var(--space-2)`. Filled bands carry `--status-on-fill`.

### 5.2 Home header

```
.orb-home__header  flex, wrap, align-items: flex-end, justify-content: space-between,
                   gap: var(--space-4)
.orb-home__title   T1 (§1.2)
.orb-home__context margin-top: var(--space-1); T2; the clock time inside is
                   .orb-home__clock { font-family: var(--font-mono);
                                      font-variant-numeric: tabular-nums lining-nums }
.orb-home__refresh ghost: transparent bg, 1px --border-subtle, --text-secondary,
                   --radius-sm, min-height 36px, padding var(--space-2) var(--space-4);
                   hover: background --bg-glass, color --text-primary;
                   disabled: --btn-disabled-{bg,text,border};
                   @media (pointer: coarse) { min-height: var(--tap-min) }
```

At ≤560px the header goes `flex-direction: column; align-items: flex-start`.

**Staleness ladder** on `.orb-home__context` (§6.5), three states, no banner.

### 5.3 Section header + count chip

```
.orb-section-header        (exists) flex, space-between, align-items: center,
                           gap: var(--space-3)
.orb-section-header__label T3, micro-caps, --text-muted. NEVER a status colour.
.orb-count-chip            min-width: 26px; padding: 2px var(--space-2);
                           border-radius: var(--radius-full);
                           font-size: var(--text-sm); font-weight: 650;
                           font-variant-numeric: tabular-nums lining-nums;
                           text-align: center;
                           background: var(--bg-glass);
                           border: 1px solid var(--border-subtle);
                           color: var(--text-secondary);
.orb-count-chip--danger    background: color-mix(in srgb, var(--status-danger) 12%, transparent);
                           border-color: color-mix(in srgb, var(--status-danger) 40%, transparent);
                           color: var(--status-danger-text);
.orb-count-chip--warn      … --status-warn …            (same shape)
.orb-count-chip--info      … --status-info …            (same shape)
```

**12%, not 20%.** 20% is the documented ceiling but leaves only 0.01–0.04 of headroom
on `--bg-elevated` (measured: 4.50–4.54:1). 12% gives 5.06–5.31:1 on every surface in
every theme. The ceiling is for emergencies, not for a component used on every screen.

Markup, so a screen reader hears a sentence and not a bare digit:

```html
<h2 class="orb-section-header__label" id="today-deteriorating">
  Deteriorating
  <span class="orb-count-chip orb-count-chip--danger">3<span class="sr-only"> patients</span></span>
</h2>
```

### 5.4 Row chassis: `.orb-row`

The single most important component. Everything on both homes that is a line of work
is one of these.

**Anatomy**

```
.orb-row
├── .orb-row__rule       (the 4px/3px left status rule, a border-left on .orb-row)
├── .orb-row__lead       flex: 0 0 56px; centred. NEWS2 badge only. Omitted entirely
│                        by sections that have no badge (alignment matters within a
│                        section, not across sections).
├── .orb-row__body       flex: 1; min-width: 0; column; gap: var(--space-1)
│   ├── .orb-row__primary    T5. May contain an inline .orb-tag before the text.
│   ├── .orb-row__secondary  T6. overflow hidden / ellipsis / nowrap (desktop).
│   └── .orb-row__meta       T7. optional third line.
└── .orb-row__trailing   flex-shrink: 0; column; align-items: flex-end;
    ├── .orb-row__value      T8, tabular
    ├── .orb-row__note       T7, --status-warn-text (e.g. "1 not charted")
    └── .orb-row__go         the `›` chevron, aria-hidden, --text-muted, 20px box
```

**Base**

```
width: 100%; text-align: left;
min-height: 56px;
display: flex; align-items: center; gap: var(--space-3);
padding: var(--space-3) var(--space-4);
background: var(--bg-surface);
border: 1px solid var(--border-subtle);
border-left: 3px solid var(--border-subtle);
border-radius: var(--radius-md);
transition: background var(--transition-fast), border-color var(--transition-fast);
```

**States**

| State | Declaration |
|---|---|
| Interactive (`--link`, is a `<button>`) | `cursor: pointer` |
| Hover | `background: var(--bg-glass); border-color: var(--border-glass);` **never `--bg-glass-hover`** (§10.2) |
| Active / press | `background: var(--bg-glass); border-color: var(--border-focus);` no transform, no scale |
| Focus-visible | global 2px `--focus-ring`, offset 2. The list container must be `overflow: visible` or the ring is clipped (§9.1) |
| Overdue / critical | `border-left: 4px solid var(--status-danger)`; `.orb-row__value` → `--status-danger-text` |
| Due soon / elevated | `border-left: 3px solid var(--status-warn)`; `.orb-row__value` → `--status-warn-text` |
| Done / given | `border-left: 3px solid var(--status-ok)` |
| Non-interactive | same box, `cursor: default`, no hover rule |
| Disabled | never. A row with nothing to do is not rendered. |

**Responsive**

```
@media (pointer: coarse) { min-height: var(--tap-min); padding-block: var(--space-4); }
@media (max-width: 768px) {
  align-items: flex-start;
  .orb-row__secondary { white-space: normal; }
  .orb-row__trailing  { /* drops into the body as a third line */ }
}
```

At ≤768px the trailing column moves **below** the body rather than shrinking. Two
implementations are acceptable: render the value into `.orb-row__meta` at that width,
or `flex-wrap: wrap` the row with `.orb-row__trailing { flex-basis: 100%;
align-items: flex-start }`. Pick one and use it on both homes.

**No hover-lift, no shadow, no scale, no colour-shift on the primary text.** A row
that moves under a descending thumb causes a mis-tap, and a mis-tapped task row on
this product is a clinical error.

### 5.5 Two list rhythms

```
.orb-list          list-style: none; display: flex; flex-direction: column;
                   overflow: visible;               /* focus rings */
                   max-width: var(--home-col-max);  /* not on the patient grid */

.orb-list--cards   gap: var(--space-2);
                   /* each .orb-row keeps its own border + radius, a discrete
                      object you complete and leave behind */

.orb-list--rows    gap: 0;
                   background: var(--bg-surface);
                   border: 1px solid var(--border-subtle);
                   border-radius: var(--radius-md);
                   overflow: hidden;                /* corner clipping */
  > li + li .orb-row { border-top: 1px solid var(--border-subtle); }
  .orb-row { background: transparent; border: none;
             border-left: 3px solid transparent; border-radius: 0; }
```

`--cards` when every row is a **discrete unit of work you act on and dismiss**
(nurse Due now, doctor Deteriorating, doctor Pending).
`--rows` when rows are **readings you scan** (doctor New results).

Note the `overflow: hidden` on `--rows` clips the focus ring on its child rows. Fix
by giving `--rows` `padding: 1px` and the inner rows `outline-offset: -2px`, or by
dropping the `overflow: hidden` and squaring the first/last row corners explicitly.
Do not ship a clipped ring.

### 5.6 Micro-caps tag: `.orb-tag`

```
display: inline-flex; align-items: center;
padding: 1px var(--space-2);
font-size: 0.7rem; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase;
line-height: 1.6;                     /* keeps the pill ≥16px tall */
border-radius: var(--radius-sm);
color: var(--status-on-fill);
```
`--obs` → `background: var(--status-info)` · `--med` → `var(--accent-warm)` ·
`--sign` → `var(--status-info)` · `--ack` → `var(--status-danger)`.

`--accent-warm` on `--med` is inherited from the existing `.task-row__kind--med` and
is kept for continuity; it is the one accent-family fill on the homes and it is a
**category tag, not a status**, which is why it is a different accent token from
`--accent-deep` (buttons) and never appears as a rule or a chip.

The tag always sits immediately before text that says the same thing in words
("OBS · Observations", "SIGN · 3 notes awaiting your signature"), so it carries no
meaning alone (§9.6).

### 5.7 State blocks

Ported, near-verbatim, from `src/components/round/RoundStates.tsx` and
`Round.css:653-720`, that component already argued this correctly and its docstring
is the doctrine:

> A blank region on a clinical screen is a hazard, because "there is nothing to
> report" and "we could not find out" look identical when both render as nothing.

**Each state has a distinct SHAPE, not just distinct words.**

```
.orb-state              display: flex; gap: var(--space-3);
                        padding: var(--space-6) var(--space-5);
                        border-radius: var(--radius-md);
                        max-width: var(--home-col-max);
.orb-state__title       T12
.orb-state__detail      T13
.orb-state__reassure    T14
.orb-state__icon        flex-shrink: 0; 22px; --text-muted

.orb-state--loading     flex-direction: column;
                        background: var(--bg-glass);
                        border: 1px solid var(--border-subtle);
.orb-state--empty       background: transparent;
                        border: 1px dashed var(--border-glass);
                        color: var(--text-secondary);
.orb-state--error       background: color-mix(in srgb, var(--status-danger) 10%, transparent);
                        border: 1px solid color-mix(in srgb, var(--status-danger) 34%, transparent);
                        border-left: 4px solid var(--status-danger);
  .orb-state__title, .orb-state__icon { color: var(--status-danger-text); }
  .orb-state__warn { T14, font-weight: 600, color: var(--status-danger-text); }
```

Roles: loading `role="status" aria-busy="true" aria-live="polite"`; empty, no role;
error `role="alert"`.

### 5.8 Skeleton

```
.orb-skeleton         display: flex; flex-direction: column; gap: var(--space-2);
.orb-skeleton__bar    height: 10px; border-radius: var(--radius-full);
                      background: var(--bg-glass-hover);
                      animation: orb-skeleton-pulse 1.4s ease-in-out infinite;

@keyframes orb-skeleton-pulse {
  0%, 100% { background: var(--bg-glass); }
  50%      { background: var(--bg-glass-hover); }
}

@media (prefers-reduced-motion: reduce) {
  .orb-skeleton__bar { animation: none; }
}
```

**The keyframe animates `background`, not `opacity`**, same reason as the house
rule, and it is the pattern `Round.css` already proved. The explicit `animation:
none` under reduced-motion is deliberate: the global kill switch sets
`animation-duration: 0.01ms` with `iteration-count: 1`, which parks the bar on its
*end* keyframe; `none` parks it on the quieter `--bg-glass`.

### 5.9 Patient card (nurse): restyle only

Keep the structure at `NursePanel.css:441-527`. Changes: type to the T-ramp
(`__name` T5→`--text-md`/700 stays, `__meta` T7, `__dx` T6, `__facts` T6), explicit
`tabular-nums` on `__facts`, and `.patient-card__allergies` keeps its 10% warn wash
(measured 5.56–6.2:1 for `--status-warn-text`, fine). Border-top status rule stays 3px.

### 5.10 Inline Sage composer: `.today-ask` (doctor only)

**Do not reuse `.sage-composer`.** `Sage.css` is imported by `SagePanel.tsx` only
(`src/pages/panels/SagePanel.tsx:15`), which is a lazy chunk. A doctor landing on
Today has never visited Sage, so `.sage-composer` resolves to *nothing* and the
composer renders as a bare textarea on the page background. This is the exact bug
class the shared-primitives rule exists for; here the correct answer is a
home-owned class, not a promotion, because the composer's geometry is genuinely
different (single-line prompt, larger type, no attachment/voice controls).

```
.today-ask            display: flex; align-items: flex-end; gap: var(--space-3);
                      padding: var(--space-3) var(--space-3) var(--space-3) var(--space-4);
                      background: var(--bg-surface);
                      border: 1px solid var(--border-glass);
                      border-radius: var(--radius-lg);
                      max-width: var(--home-col-max);
                      transition: border-color var(--transition-fast),
                                  box-shadow var(--transition-fast);
.today-ask:focus-within {
                      border-color: var(--accent-gold);
                      box-shadow: 0 0 0 3px var(--accent-glow);
}
.today-ask__input     flex: 1; min-width: 0; background: transparent; border: none;
                      outline: none; resize: none;
                      font-family: var(--font-family);
                      font-size: var(--text-base); line-height: 1.5;
                      color: var(--text-primary);
                      min-height: 24px; max-height: 120px;
                      padding: var(--space-1) 0;
  ::placeholder       color: var(--text-muted);
.today-ask__send      flex-shrink: 0; width: 40px; height: 40px;
                      display: flex; align-items: center; justify-content: center;
                      background: var(--accent-deep); color: var(--btn-primary-text);
                      border-radius: var(--radius-sm);
  :hover              background: var(--btn-gradient-hover);
  :active             filter: brightness(0.96);
  :disabled           background: var(--btn-disabled-bg); color: var(--btn-disabled-text);
                      cursor: not-allowed;     /* never opacity */
@media (pointer: coarse) { .today-ask__send { width: var(--tap-min); height: var(--tap-min); } }
```

No `box-shadow: var(--elev-1)`, cards get borders, not shadows (`DESIGN.md` §3).
The heavier `--border-glass` is what distinguishes it as an input.

The inner textarea suppresses its own focus ring because the wrapper shows
`:focus-within`. Add `.today-ask` to the existing `:where(...)` list in
`src/index.css:312`, do not write a new rule.

Behaviour: Enter submits, Shift+Enter newline, Escape blurs. Submit navigates via the
existing bus (`gotoTab('sage')`) carrying the question. **A new bus event is
required** (`orb:sage-ask`, `detail: { question: string }`), that is a code
contract, not a visual decision, and it belongs beside the existing events in
`src/lib/orbBus.ts`.

### 5.11 Round resume row: `.today-resume`

Text only. Uses `.orb-row` unchanged; `.today-resume` adds nothing but the trailing
label styling. See §3.4 for the rejected progress bar.

### 5.12 Totals strip (nurse): restyle

`.nurse-totals` keeps `flex-wrap: wrap; gap: var(--space-2)`. `.nurse-total` becomes
a `.orb-count-chip` with a T7 label beside a T11 number. `12 patients` renders as a
plain T2 span, not a pill. Order is fixed in markup and never sorted by value.

---

## 6. Every state, designed

**Two laws, above all the detail below:**

1. **A section that failed must never render as empty.** Failure gets `.orb-state
   --error` with the real message and the sentence *"This is a display failure, not
   an all-clear."* This is the single most important state rule on either home.
2. **An empty state must name what it searched, and when.** "Nothing here" is a
   claim; "no patient scored above 4 as of 09:14, across 12 patients" is evidence.
   The difference is whether a tired reader can trust it.

States are **per section**, not per page. If `/chart/unseen` 500s while the ward
board loads, only New results shows an error.

### 6.1 Loading

- **Skeletons only, never a spinner, never a full-page "Loading…".**
- The skeleton's geometry **equals the loaded row's geometry**: same `min-height`
  (56 / `--tap-min` coarse), same 56px lead box (a `--radius-md` rectangle), body
  bars at 42% / 64%, trailing bar 56×10. Nothing shifts when data lands.
- **Row count = the number of rows this section rendered last time, held in component
  state, defaulting to 3.** In memory only, no storage, no PHI, and it survives tab
  switches because the shell keeps visited panels mounted. First load skeletons 3;
  every subsequent load skeletons exactly right.
- **Skeletons appear on first load only.** A background poll (60s) must never replace
  content with skeletons, `loading && !data` shows skeletons; `loading && data`
  updates in place and shows nothing but the Refresh control's busy state. This is
  already how `NursePanel` behaves; it is now law for both homes.
- `role="status" aria-busy="true"` on the section, with a `.sr-only` label
  ("Loading your shift", "Loading deteriorating patients").

### 6.2 Empty: the nurse's "nothing due"

**This is a good state and must be designed as one.** It is not the dashed grey box
that also means "we found nothing". A nurse who reaches a quiet moment should feel
the screen confirm it, not apologise for it.

```
┌────────────────────────────────────────────────────────────┐
│   ⊙   Nothing due right now                                │  T12
│       Next observations for GM-07 at 10:30.                │  T13, time in mono
│                                                            │
│       Live view of General Medicine, 12 patients,         │  T14
│       updated 09:14. Nothing failed to load.               │
└────────────────────────────────────────────────────────────┘
```

- Container: `.orb-state--empty` **plus** `.orb-state--calm`, which overrides the
  dashed border to a **solid** `1px --border-subtle` on `--bg-surface`. Dashed says
  *absence*; solid says *this is the answer*.
- Icon: 24px check in a 36px circle, `color: var(--status-ok-text)`,
  `background: color-mix(in srgb, var(--status-ok) 12%, transparent)`. **The only
  green on the nurse home.** Measured `--status-ok-text` on a 12% ok tint, across all
  four surfaces: 5.25–6.85:1 (dark), 5.08–5.75 (light), 10.10–13.08 (night).
- **Line 2 is load-bearing**: the next thing that will become due, with its bed and
  its clock time. It converts "nothing" into "nothing *yet*, and here is when", which
  is what makes an empty queue trustworthy rather than suspicious. If no future due
  time is known, line 2 reads "No observations or doses are scheduled in the next
  four hours.", never omitted.
- Line 3 names the scope, the count and the freshness, and states plainly that
  nothing failed.
- **No exclamation marks, no emoji, no congratulation.** Rejected: "You're all caught
  up! 🎉". A nurse at 3am does not want to be congratulated by software; she wants to
  know the screen is telling the truth.
- Padding `--space-6` block; content column `max-width: 480px`; icon and text on one
  row, left-aligned (not centred, centred text reads as a marketing state).

### 6.3 Empty: the rest

Same block, `--calm` only where the emptiness is an achievement.

| Section | Title | Detail | Reassurance |
|---|---|---|---|
| Deteriorating (calm) | `No patient is scoring above threshold.` | `All 12 patients on General Medicine are at NEWS2 4 or below as of 09:14.` | `Nothing failed to load.` |
| Pending my decision (calm) | `Nothing is waiting on you.` | `0 notes to sign · 0 unresolved safety alerts · ward round finished 09:14.` | n/a |
| New results (calm) | `No new results since you last looked.` | `Last checked 08:41.` | `Nothing failed to load.` |
| My patients (plain empty, dashed) | `No admitted patients in this scope.` | `Your ward scope is General Medicine. If you expect patients here, this may be a scope problem, not an empty ward.` | `Nothing failed to load.` |

Note the Pending row: **naming the three things that were checked and found empty**
is what makes the emptiness a statement rather than a shrug.

### 6.4 Error

`.orb-state--error`, `role="alert"`, per section:

```
⚠  Couldn't load new results
   {the real error message from the API}
   This is a display failure, not an all-clear. Do not read the missing
   information as normal, check the chart directly.
   [ Retry ]
```

Retry is a `.orb-home__refresh`-styled ghost button scoped to that section.
Never a bare "Something went wrong". Never a toast, a toast for a failed clinical
read disappears before it is believed.

### 6.5 Offline and stale: two different things

**Offline** is `OfflineBanner` + `useOfflineSnapshot`, unchanged and mandatory. Both
homes pass their board *through* the snapshot so a cached copy can never render
without the banner that dates it. The doctor home adopts this pattern for its Vigil
slice; the aside blocks (New results, End of shift) render `.orb-state--empty` with a
"not available offline" detail rather than stale numbers.

**Stale** is a *live* view that has stopped refreshing while online, a failed poll,
a sleeping tablet. No banner: a banner for a four-minute-old board is crying wolf.
A three-step ladder on `.orb-home__context` instead:

| Age of `generated_at` | Treatment |
|---|---|
| < 3 min | `updated 09:14`, `--text-muted`, mono clock. |
| 3–10 min | `updated 09:14 · 6 min ago`, the whole line switches to `--status-warn-text`, `role="status"`. |
| > 10 min | `updated 09:14 · 14 min ago` + inline `[ Retry ]` ghost button, `role="alert"`. |

The clock time never disappears, a nurse can compare it against her watch, which is
the whole reason `OfflineBanner` prints one.

### 6.6 Permission-denied

**A permission the account does not hold produces absence, not a disabled control.**
A greyed-out block is a promise you are withholding; absence is honest, and a
403-on-click is the defect this whole document family exists to kill.

- Doctor without `outcome.record` → the End of shift block does not render. No lock
  icon, no explanatory grey card.
- Where absence would confuse someone who has seen a colleague's screen or the
  training deck, and only there, a single T14 line is permitted in place of the
  block: *"Outcome capture is recorded by the charge nurse on this ward."* No border,
  no icon, no control.
- **If a server 403 arrives at runtime anyway** (the client permission mirror in
  `src/lib/roles.ts` has drifted), that is `.orb-state--error`, not silence:
  *"Orb refused this read for your account. This is a display failure, not an
  all-clear."* A silently empty section after a 403 is indistinguishable from a calm
  ward, and that is the failure mode this product cannot have.

---

## 7. Motion

Purposeful only. `--transition-fast` 140ms · `--transition-base` 220ms ·
`--transition-slow` 360ms, all `cubic-bezier(0.3, 0, 0.2, 1)`.

### Permitted

| What | Duration | Easing | Note |
|---|---|---|---|
| Home root entrance | `animate-fade-in` 0.25s | ease-out | Opacity only, no travel. Plays once, the shell keeps panels mounted. |
| Row hover / press | `--transition-fast` | token | `background` + `border-color` only. |
| Refresh button state | `--transition-fast` | token | colour only. |
| Sage composer focus | `--transition-fast` | token | `border-color` + `box-shadow`. |
| Skeleton pulse | 1.4s infinite | ease-in-out | `background` swap, §5.8. |
| Offline / sync dot | existing | existing | Unchanged shell behaviour. |

### Forbidden: every one of these is a specific failure

- **No animated numbers.** No count-up, no odometer, no tween on any value. A NEWS2 of
  9 is 9 the instant it paints. A changing vital is not a delight moment; it is the
  most important fact on the screen, and animating it delays it and implies the
  intermediate values existed.
- **No row reorder animation** (no FLIP, no transition on `transform`). When a patient
  deteriorates and the sort changes, the list re-renders instantly. A row sliding
  under a descending thumb causes a mis-tap, and a mis-tapped task row is a clinical
  error.
- **No stagger on section entrance.** `.stagger-*` exists; it is not used here. It
  makes the page assemble itself in front of a tired reader for no information gain.
- **No auto-scroll** to a "most urgent" item. The reader owns the scroll position.
- **No pulsing, flashing or breathing on danger rows.** This is the classic
  alarm-fatigue mechanism and it is prohibited outright.
- **No hover-reveal** of any control, label or value.
- **No skeleton→content crossfade.** It makes the swap feel slower and hides layout
  shift that should have been fixed instead.
- **No progress-bar width transitions** (§3.4).
- **No `translateY`, `scale` or shadow change on hover.** House rule; restated because
  it is the most-violated one.

### Reduced motion

The global `prefers-reduced-motion` block zeroes durations and stagger delays. The
homes add exactly one rule of their own, `.orb-skeleton__bar { animation: none }`
(§5.8), because the global rule parks the bar on the wrong keyframe. Nothing else
on either home needs a reduced-motion branch, because nothing else moves.

---

## 8. Dark, light, night

Themes flip entirely at the token layer. The homes add **one** `[data-theme]`
override each, for one true semantic reason.

### The one override

```css
[data-theme="night"] .orb-row,
[data-theme="night"] .orb-list--rows,
[data-theme="night"] .orb-state {
  border-color: var(--border-glass);
}
```

**Why.** Night's canvas is `#000000` and its card surface is `#101010`, a luminance
step of about 0.6%. In every other theme the surface itself separates a card from the
canvas and the border is a refinement; in night the **1px border is the only thing
that exists**, and `--border-subtle` (`rgba(255,245,225,0.14)`) on a screen dimmed for
a dark ward can vanish. `--border-glass` (0.22) restores the edge. Borders carry no
text, so there is no contrast pair to add to the gate; this must be verified by eye
in the night captures.

The status left-rules are unaffected, they are saturated and 3–4px.

### What is otherwise identical

Everything. No per-theme sizes, weights, spacings, radii, or motion. If a build agent
finds itself writing a second `[data-theme]` rule, the token is wrong, not the
component.

### Reading Today at 03:00 (night)

A true-black page. The Fraunces "Today" in `#ffffff` and one line of `--text-muted`
context. Three micro-caps grey headings. Rows are `#101010` slabs, edges drawn by a
22%-warm hairline, so the page reads as a stack of cards rather than floating text.
If nobody is deteriorating there is **no saturated colour on the screen at all**, 
just warm greys on black, which is exactly right for a corridor at 3am. If two
patients are, then two `#ff5e5e` NEWS2 badges with ink numerals are the only
saturated pixels in the doctor's field of view, and they are unmissable without a
single pixel of animation. The Sage send button (`#e07b1a` with `#1a0f00` ink) is the
one warm block at the bottom; the amber is low-blue-light by design, and because the
accent appears nowhere else on the page it cannot be confused with the amber warn
chips (§1.7).

### Reading My Shift at 03:00 (night)

Same page structure. The totals strip is four hairline pills, of which usually one or
two carry a 12% wash, a warm amber for doses, a dim red for obs, and the rest are
neutral. Below it, the overdue list: black slabs with a 4px `#ff5e5e` rule down the
left and a bright NEWS2 block at the head of each. The lateness value on the right
(`--status-danger-text`, `#ff6060`, 6.43:1 on the row) is the number she is actually
reading. On a quiet night the whole screen is the calm empty state (§6.2): one small
green check, three lines of text, and the next due time. **A nurse should be able to
tell a quiet ward from a busy one from three metres away, in the dark, without
reading a word**, and on this design she can, because a busy ward has saturated
left rules and a quiet one has none.

---

## 9. Accessibility

### 9.1 Focus

- One global convention: `:focus-visible` → 2px `--focus-ring`, 2px offset
  (`src/index.css:303`). **No component on either home may override it**, except the
  composer wrapper, which suppresses its inner ring via the existing `:where()` list.
- **The ring must not be clipped.** `.orb-list` is `overflow: visible`; the panel's
  own scroll container is `.orb-panel`. `.orb-list--rows` uses `overflow: hidden` for
  corner clipping and therefore needs `padding: 1px` + `outline-offset: -2px` on its
  child rows, or its corners squared instead. Verify in a real browser, not by
  reading.
- Focus-ring contrast against every surface it outlines is already gated at ≥3:1
  (measured 3.48–4.86 dark, 4.08–5.05 light, 8.66–11.91 night).

### 9.2 Keyboard order

**Today:** `h1` (not focusable) → Refresh → Deteriorating rows in visual order → "See
all patients" → Pending rows in order → New results rows → End of shift row → Sage
textarea → Sage send. Section headers are not focusable. No tab traps, no roving
tabindex (this is a list, not a widget). Source order equals visual order at every
breakpoint, **no `order:` property is used anywhere on either home**, precisely so
this stays true.

**My Shift:** Refresh → view tablist (**one** tab stop, arrows move, existing
`rovingTabKeyDown`) → the active panel's content in order. Unchanged.

### 9.3 Count chips

The number alone is meaningless to a screen reader. Every chip carries a `.sr-only`
unit inside it (§5.3): `<span class="orb-count-chip">3<span class="sr-only">
patients</span></span>` reads as *"Deteriorating 3 patients"*. Units by section:
patients · tasks · notes · results · doses.

The totals strip chips do the same: *"1 critical"*, *"2 observations overdue"*,
*"1 dose overdue"*, never *"1"*.

### 9.4 Status colour, announced

`News2Badge` currently carries its meaning in a `title` attribute only, a
hover-only affordance, invisible to touch and inconsistently announced. **Add an
`aria-label`** carrying the same sentence, and keep the `title` for pointer users:

```
aria-label="NEWS2 9, critical" + ", a single parameter scores 3" + ", SpO₂ Scale 2"
```

And for the null case: `aria-label="NEWS2 not recorded"` (the visible `, ` announces
as nothing).

Deep-link rows name their destination, since the visible `›` is `aria-hidden`:

```
aria-label="Anita Rao, bed GM-04, NEWS2 9 critical, 2 new results, open patient"
aria-label="3 notes awaiting your signature, open Scribe"
```

### 9.5 Live regions: announce transitions, not summaries

`.nurse-totals` is `role="status"` today. With a 60-second poll that re-announces the
entire ward summary once a minute, forever. **Remove it.** A summary is not a change
announcement.

Replace with one `.sr-only` `role="status" aria-live="polite"` region per home that
speaks **only on an adverse increase**, at most once per poll:

- Nurse: *"2 tasks are now overdue."* (fires when `overdueTasks.length` increases)
- Doctor: *"3 new results since you last looked."* (fires when the unseen total
  increases)

Decreases are silent. Nothing announces on a poll that changed nothing.

### 9.6 Colour is never the only carrier

Every status-coloured element is accompanied by a word in the same reading unit:

| Signal | Colour | Word | Position |
|---|---|---|---|
| Overdue | 4px danger rule | "42 min late" in the trailing value | under the OVERDUE header |
| Due soon | 3px warn rule | "in 18 min" | under the DUE WITHIN 30 MINUTES header |
| Deteriorating | filled NEWS2 badge | the NEWS2 integer + `aria-label` band name | first section |
| Adverse count | 12% chip tint | the chip's `.sr-only` unit | beside its label |

For deuteranopia specifically, red and amber left rules are the weak pair. They are
separated by **rule weight (4px vs 3px)**, by **section membership**, and by the
trailing words ("late" vs "in"). Dashed is deliberately not used as a differentiator
it already means *model-estimated* (`DESIGN.md` §5) and *empty* (§5.7).

### 9.7 Headings and landmarks

Home title is `<h1 class="orb-home__title">`; section labels are `<h2>`; lists are
`<ul>` with `aria-labelledby` pointing at their `<h2>`; rows are `<li>` containing a
`<button>`.

> **Known deviation, flagged not fixed:** the shell already renders an `<h1>`
> (`.shell__page-title`, "Orb Today"), so both homes produce a second `<h1>`. Two
> `<h1>`s is a best-practice violation, not a WCAG failure, and fixing it means
> changing the heading level of the shell title for all seventeen tabs. Out of scope
> tonight; recorded here so it is a decision rather than an oversight.

---

## 10. Contrast: the pairs this spec relies on

All figures measured with the same WCAG 2.1 maths `scripts/check-contrast.mjs` uses.

### 10.1 Pairs already gated (no action needed)

| Pair | dark | light | night |
|---|---|---|---|
| `--text-primary` on `--bg-surface` | 12.32 | 14.73 | 19.03 |
| `--text-secondary` on `--bg-surface` | 7.44 | 7.46 | 12.64 |
| `--text-muted` on `--bg-surface` | 5.63 | 5.58 | 7.24 |
| `--text-muted` on `--bg-surface` + `--bg-glass` | 4.78 | 5.12 | 6.35 |
| `--status-danger-text` on `--bg-surface` | 6.35 | 7.12 | 6.43 |
| `--status-warn-text` on `--bg-surface` | 7.00 | 6.66 | 11.66 |
| `--status-ok-text` on 12% ok tint over `--bg-surface` | 5.49 | 5.75 | 11.12 |
| `--status-danger-text` on 12% danger tint over `--bg-surface` | 5.31 | 5.83 | 5.61 |
| `--status-warn-text` on 12% warn tint over `--bg-surface` | 5.56 | 5.64 | 9.28 |
| 12% tint, worst case (any status, any of the 4 surfaces) | 5.06 | 5.00 | 5.14 |
| `--status-on-fill` on `--status-danger` / `--status-warn` | pass | pass | pass |
| `--btn-primary-text` on `--accent-deep` | pass | pass | pass |
| `--btn-disabled-text` on `--btn-disabled-bg` | pass | pass | pass |
| `--focus-ring` on every surface (≥3:1) | 3.48–5.40 | 4.08–5.05 | 8.66–11.91 |

Every one of these is in the 378-pair palette pass today and stays green.

### 10.2 A pair the gate cannot see: and a live defect

The checker composites **`--bg-glass`** over each base surface. It never composites
**`--bg-glass-hover`**, because no rule declares a `color` alongside it, hover rules
set only `background`, so no pair is formed and the call-site pass skips it.

Measured, `--bg-glass-hover` over `--bg-surface`:

| Text token | dark | light | night |
|---|---|---|---|
| `--text-primary` | 9.12 | 12.69 | 14.53 |
| `--text-secondary` | 5.51 | 6.43 | 9.66 |
| **`--text-muted`** | **4.17 ✗** | 4.81 | 5.53 |
| **`--accent-text`** | **4.16 ✗** | 5.66 | 9.60 |
| `--status-danger-text` | 4.70 | 6.14 | 4.91 |
| `--status-warn-text` | 5.18 | 5.74 | 8.91 |

**Consequences, and the rules that follow:**

1. `.task-row:hover` (`NursePanel.css:365`) sets `background: var(--bg-glass-hover)`
   and contains `.task-row__patient` and `.task-row__detail` in `--text-muted`. In
   dark theme, hovering any task row drops both to **4.17:1**. Same for
   `.handover__toggle:hover` → `.handover__meta`. **Both are live AA failures today.**
2. **Rule: `--bg-glass-hover` may not sit under `--text-muted` or `--accent-text`.**
   Row hover on both homes is `background: var(--bg-glass)` +
   `border-color: var(--border-glass)` (§5.4). The lighter fill still marks the hover
   and holds 4.78:1. This is the same reasoning already recorded at
   `ClinicianShell.css:231-233` for `.shell__user-security`.
3. **Gate follow-up (not tonight):** add `--bg-glass-hover` to the composited surface
   list in `scripts/check-contrast.mjs`, so this class of defect is caught by the
   palette pass instead of by hand. It will surface the two existing failures above;
   fix them in the same change.

### 10.3 New tint percentages introduced

None outside 12%, which is inside the existing 6/12/20 set the gate already measures.

---

## 11. Build order and file map

| # | File | Action | Blocking? |
|---|---|---|---|
| 1 | `src/index.css` | add `--home-col-max: 960px` to `:root`; add `.today-ask` to the `:where()` focus-suppression list at line 312 | yes, everything depends on the token |
| 2 | `src/components/News2Badge.tsx` | moved from `src/pages/panels/nurse/`; add `aria-label` (§9.4); update 3 importers | yes, doctor home needs it |
| 3 | `src/pages/ClinicianShell.css` | add §5.2–§5.8 primitives; **move** `.news2*` in from `NursePanel.css` | yes |
| 4 | `src/pages/panels/doctor/DoctorHome.tsx` + `DoctorHome.css` | new lazy panel; §3 | n/a |
| 5 | `src/lib/orbBus.ts` | add `orb:sage-ask` (§5.10) | needed by 4 |
| 6 | `src/pages/panels/nurse/NursePanel.tsx` + `.css` | apply the §4.4 diff **only**; respect §4.5 | n/a |
| 7 | `src/i18n/locales/{en,hi}.json` | `nav.today`, `subtitle.today`; update `subtitle.shift` if the copy changed | n/a |
| 8 | `tests/visual/driver.cjs` | add `doctor/today`, `nurse/shift`, `night/nurse-shift`, `responsive/today-tablet` scenarios | see §12.8 |

### Definition of done

- [ ] `node scripts/check-contrast.mjs` green (378 palette + call sites).
- [ ] No `opacity` in either home's stylesheet, for any purpose.
- [ ] No `--bg-glass-hover` under `--text-muted` or `--accent-text` anywhere.
- [ ] No `translateY`, `scale`, `box-shadow` or `backdrop-filter` on either home.
- [ ] No `[data-theme]` rule in either home stylesheet except the single night border
      override in §8.
- [ ] Every numeric element declares `font-variant-numeric: tabular-nums lining-nums`.
- [ ] No text below `--text-sm` that is a sentence; nothing below 0.7rem at all.
- [ ] Every interactive element ≥24px, ≥44px at `pointer: coarse`; verified by
      resizing, not by reading the CSS.
- [ ] Keyboard tab order equals visual order at 1440, 1024, 768 and 390px wide.
- [ ] Every section renders correctly in all six states: loading, empty, error,
      offline, stale, permission-denied.
- [ ] Screenshots in dark, light and night at 1440 / 1024 / 768 / 390.

---

## 12. What cannot be made premium in one night

Flagged honestly rather than shipped half-done.

1. **The nurse-shaped patient sheet.** Phase 2 in `DUAL_EXPERIENCE_DESIGN.md`. Until
   it exists, "My patients" still deep-links into Vigil's prescriber-shaped chart, 
   a doctor's surface with add-medication and discharge on it. The homes must not
   paper over this with a partial read-only sheet; a half-built PHI surface is worse
   than a correctly-shaped detour. Leave the deep-link honest.
2. **The shell rail's icon-only mode at ≤1024px.** Ten unlabelled icons whose labels
   are `title` attributes, hover-only, unreachable on the exact device the nurse
   uses. This is the largest remaining usability defect in the nurse lane and it is
   **not** fixable from a home stylesheet. It needs the `shellConfig` extraction and
   the rail trim, which is blocked on the Q1 answer. **Do not let either home
   compensate by growing its own navigation**, that is how two navigation systems
   are born.
3. **`.orb-panel-loading` has no CSS anywhere in the repo.** Grep confirms zero
   matching rules across every stylesheet. Every panel's Suspense fallback, the
   stale-chunk "Updating…" message and several error states currently render as
   unstyled body text on the canvas. It is a small fix and a cross-cutting one, it
   changes the first paint of all seventeen tabs, so it should be done deliberately
   with the visual gate, not as a side effect of building two homes. The homes must
   not depend on it.
4. **Real-time.** Both homes poll at 60 seconds. Making "deteriorating" genuinely
   live means wiring the realtime channel that `initCriticalAlerts()` already uses,
   and that path is on the pilot's critical list. Ship polling; make the staleness
   ladder (§6.5) honest instead. Do not fake liveness with a pulsing dot.
5. **Doctor-home personalisation** ("my patients" vs the ward). There is no
   patient-assignment concept in Orb. Do not invent one pre-pilot; `Today` is
   ward-scoped and says so in its context line.
6. **Hindi typography.** `hi.json` exists, but Fraunces has no Devanagari and Inter
   Variable ships none either, so a Hindi `Today` title falls through
   Fraunces → Georgia → Times → whatever system Devanagari face the tablet has, at a
   different optical size and weight. This is a pre-existing, app-wide condition, not
   a homes problem, but the homes make it visible because the title is the biggest
   text on the page. Tonight's stopgap is one rule,
   `[lang="hi"] .orb-home__title { font-family: var(--font-family); }`, so the Hindi
   title is at least consistent with the rest of the UI. Choosing a real Devanagari
   display face is a separate decision with a real download-size cost.
7. **The copy is not clinician-reviewed.** Every string in §6 is written to a
   doctrine (name what you searched; never imply an all-clear) but in an engineer's
   voice. These sentences will end up in training materials at Weeks 9–10 per the
   roadmap, and training must never describe a screen or a phrase that moved last
   week. Get the champion to read §6.2 and §6.3 specifically, the empty states are
   where a nurse decides whether to trust the software.
8. **The visual gate does not cover the nurse home at all.** `tests/visual/driver.cjs`
   has exactly one nurse scenario (`nurse/vigil`), and it waits on `.patient-row`, 
   i.e. it navigates to the patient board, not to My Shift. **The nurse home has never
   been captured, in any theme.** Adding four scenarios across three themes with real
   fixture states (populated, empty, error, offline) is several hours of fixture work,
   not fifteen minutes. Either budget it or ship the homes knowingly ungated and say
   so out loud, an ungated surface in a three-theme system regresses within two
   weeks, and this one has been ungated since it was built.
