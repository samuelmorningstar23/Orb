/**
 * STYLESHEET OWNERSHIP IS A CORRECTNESS INVARIANT, NOT A STYLE PREFERENCE.
 *
 * Every panel stylesheet ships inside that panel's lazy chunk. A class used by
 * two panels but styled inside one of them renders UNSTYLED until that panel is
 * visited. This repo has shipped that bug twice, and the third instance was
 * live until tonight: `.news2*` lived in NursePanel.css, and a doctor — who has
 * no `shift` tab at all — would have seen the NEWS2 badge as a bare number with
 * no box, no border and no colour band. The band IS the clinical signal.
 *
 * A rendering test cannot catch this. Vitest runs with `css: false`, jsdom
 * computes no cascade, and even in a browser the bug only appears in a session
 * that has NOT visited the owning panel. So the invariant is asserted where it
 * actually lives: in the files.
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')

const SHELL = read('./ClinicianShell.css')
const NURSE = read('./panels/nurse/NursePanel.css')

/** Comments are stripped so a class NAMED in prose (the "moved out" note left
 *  behind in NursePanel.css) is never mistaken for a class STYLED there. */
const stripComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, '')

/** True when `css` contains at least one rule whose selector mentions `cls`. */
function styles(css: string, cls: string): boolean {
  const escaped = cls.replace(/[.*+?^${}()|[\]\\-]/g, '\\$&')
  // selector text (no braces/semicolons) mentioning the class, then a body
  return new RegExp(`(^|[,{}\\s])[^{};]*${escaped}(?![\\w-])[^{};]*\\{[^{}]*[a-z-]+\\s*:`, 'm')
    .test(stripComments(css))
}

/* Every class both homes consume. The doctor home and the nurse home are built
   by different agents; this list is the contract between them. */
const SHARED_PRIMITIVES = [
  '.news2', '.news2__label', '.news2__value',
  '.news2--critical', '.news2--elevated', '.news2--stable', '.news2--none', '.news2--lg',
  '.orb-home__header', '.orb-home__title', '.orb-home__context', '.orb-home__clock', '.orb-home__refresh',
  '.orb-section-header__label', '.orb-count-chip', '.orb-count-chip--danger',
  '.orb-count-chip--warn', '.orb-count-chip--info',
  '.orb-row', '.orb-row__lead', '.orb-row__body', '.orb-row__primary', '.orb-row__secondary',
  '.orb-row__meta', '.orb-row__trailing', '.orb-row__value', '.orb-row__note', '.orb-row__go',
  '.orb-row--overdue', '.orb-row--soon', '.orb-row--done', '.orb-row--link',
  '.orb-list', '.orb-list--cards', '.orb-list--rows',
  '.orb-tag', '.orb-tag--obs', '.orb-tag--med', '.orb-tag--sign', '.orb-tag--ack',
  '.orb-state', '.orb-state__title', '.orb-state__detail', '.orb-state__reassure',
  '.orb-state__icon', '.orb-state__warn',
  '.orb-state--loading', '.orb-state--empty', '.orb-state--calm', '.orb-state--error',
  '.orb-skeleton', '.orb-skeleton__bar',
]

describe('shared home primitives live in the eager shell stylesheet', () => {
  it.each(SHARED_PRIMITIVES)('%s is styled in ClinicianShell.css', cls => {
    expect(styles(SHELL, cls)).toBe(true)
  })

  it('the NEWS2 badge is no longer styled inside the nurse lazy chunk', () => {
    for (const cls of ['.news2', '.news2__value', '.news2--critical']) {
      expect(styles(NURSE, cls), `${cls} is back in NursePanel.css`).toBe(false)
    }
  })

  /* `.orb-panel-loading` is the Suspense fallback for all seventeen tabs, the
     stale-chunk "Updating…" note and several panel error states. It had ZERO
     matching rules anywhere in the repo — every one of them painted as unstyled
     body text on the canvas. */
  it('the Suspense fallback and the panel error boundary are styled at all', () => {
    /* The whole rule, not merely "some selector mentions it": a single
       [data-theme] border override would satisfy a looser check while the
       block itself still painted as bare body text on the canvas. */
    for (const cls of ['.orb-panel-loading', '.orb-panel-error']) {
      const block = new RegExp(`\\n\\${cls} \\{([^}]*)\\}`).exec(stripComments(SHELL))?.[1] ?? ''
      expect(block, cls).toMatch(/padding:/)
      expect(block, cls).toMatch(/background:/)
      expect(block, cls).toMatch(/border(-radius)?:/)
    }
  })
})

describe('house rules the primitives must obey', () => {
  /* From the first `SHARED HOME PRIMITIVES` banner to the end of the file. */
  const primitives = stripComments(SHELL.slice(SHELL.indexOf('SHARED HOME PRIMITIVES')))

  it('never expresses de-emphasis or state with opacity', () => {
    // It multiplies down the tree and fades label and fill by the same factor,
    // so contrast can only get worse. Neutral tokens instead, always.
    expect(primitives).not.toMatch(/(^|[;{\s])opacity\s*:/)
  })

  /* --bg-glass-hover under --text-muted measures 4.17:1 in dark. The skeleton
     bar is the one legitimate use: it is a grey bar with no text in it. */
  it('uses --bg-glass-hover only where no text sits on it', () => {
    const users = primitives
      .split('}')
      .filter(block => /background[^;]*var\(--bg-glass-hover\)/.test(block))
      .map(block => block.split('{')[0].trim())
    expect(users.sort()).toEqual(['.orb-skeleton__bar', '50%'].sort())
  })

  it('carries the row hover fill the contrast fix requires', () => {
    expect(primitives).toMatch(/\.orb-row--link:hover[\s\S]{0,160}background:\s*var\(--bg-glass\)/)
  })

  it('declares tabular figures on every numeric primitive', () => {
    // A count that jitters 1px wider when it ticks 9 → 10 is how a calm screen
    // becomes a twitchy one; none of these class names match the global
    // tabular-nums selectors, so each must say so itself.
    for (const cls of ['.orb-count-chip', '.orb-row__value', '.orb-row__meta', '.news2__value']) {
      const block = primitives.slice(primitives.indexOf(`\n${cls} {`))
      expect(block.slice(0, block.indexOf('}')), cls).toMatch(/font-variant-numeric:\s*tabular-nums/)
    }
  })

  /* Micro-caps tags are the smallest text permitted anywhere on either home,
     and only because each one duplicates an adjacent word. */
  it('has no text below the 0.7rem floor', () => {
    const tooSmall = [...primitives.matchAll(/font-size:\s*(0\.\d+)rem/g)]
      .map(m => parseFloat(m[1]))
      .filter(size => size < 0.7)
    expect(tooSmall).toEqual([])
  })
})

describe('the two live hover contrast defects the spec found', () => {
  /* Both sit under --text-muted, which drops to 4.17:1 on --bg-glass-hover in
     dark. The palette gate could not see either: a hover rule declares only a
     background, so no colour/background pair was ever formed. */
  it('the nurse task row hovers on the lighter fill', () => {
    expect(NURSE).toMatch(/\.task-row:hover\s*\{[^}]*background:\s*var\(--bg-glass\)[^}]*\}/)
    expect(NURSE).not.toMatch(/\.task-row:hover\s*\{[^}]*--bg-glass-hover/)
  })

  it('so does the handover toggle', () => {
    expect(NURSE).toMatch(/\.handover__toggle:hover\s*\{[^}]*background:\s*var\(--bg-glass\)[^}]*\}/)
    expect(NURSE).not.toMatch(/\.handover__toggle:hover\s*\{[^}]*--bg-glass-hover/)
  })
})

/* ──────────────────────────────────────────────────────────────────────────
 * THE PILLAR RAIL MUST LOOK SCROLLABLE WHEN IT IS.
 *
 * There are 20+ pillars. On a 720px-tall laptop the last two — Admin among
 * them — sit below the fold. The container already scrolled, but it carried
 * `::-webkit-scrollbar { width: 0 }`, and on macOS the overlay scrollbar is
 * invisible until something moves: the rail simply ended. A whole pillar was
 * unreachable-looking, which is the most likely reason a full UI sweep never
 * opened the admin screens at all.
 *
 * jsdom computes no cascade, so — like the ownership rules above — this is
 * asserted where it lives, in the stylesheet.
 * ────────────────────────────────────────────────────────────────────────── */
describe('the rail says when there is more below the fold', () => {
  const railRule = stripComments(SHELL)
    .match(/\.shell__sidebar-top\s*\{[^}]*\}/)![0]

  it('is a scroll container', () => {
    expect(railRule).toMatch(/overflow-y:\s*auto/)
    expect(railRule).toMatch(/min-height:\s*0/)
  })

  it('no longer hides its own scrollbar in the vertical rail', () => {
    // The suppression that caused this: a zero-width scrollbar on the rail.
    expect(stripComments(SHELL))
      .not.toMatch(/\.shell__sidebar-top::-webkit-scrollbar\s*\{[^}]*width:\s*0/)
  })

  it('carries a scroll shadow that only shows while there is more to scroll', () => {
    // `local` covers travel with the content; `scroll` shadows stay pinned to
    // the viewport and are hidden by the covers at each end. Both halves are
    // required — one without the other is a fade that lies at the extremes.
    expect(railRule).toMatch(/local/)
    expect(railRule).toMatch(/scroll/)
    expect((railRule.match(/local/g) || []).length).toBe(2)
    expect((railRule.match(/scroll/g) || []).length).toBe(2)
  })

  it('leaves scrollbar APPEARANCE to the single global rule in index.css', () => {
    // index.css styles ::-webkit-scrollbar once, for the whole product. The
    // shell may switch a scrollbar off for a specific strip; it may not fork
    // the look and start a second convention.
    const shell = stripComments(SHELL)
    expect(shell).not.toMatch(/::-webkit-scrollbar-thumb/)
    expect(shell).not.toMatch(/::-webkit-scrollbar-track/)
    expect(shell).not.toMatch(/scrollbar-color/)
  })
})
