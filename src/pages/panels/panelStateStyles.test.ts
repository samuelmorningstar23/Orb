/**
 * STATE IS A TOKEN, NEVER A FADE, AND A STATE CLASS MUST WIN ITS CASCADE.
 *
 * Opacity fades a label together with its fill, so a disabled primary button
 * at 0.55 left its label near-invisible in dark, and a dot pulsing to 40% reads
 * as a resolved alert. Separately, a state or utility class that loses on
 * specificity is a state nobody sees: hovering the selected tab erased it, and
 * `.pay-table td` pinned every amount column left under `.pay-td--num`.
 *
 * Vitest runs with css: false and jsdom computes no cascade, so both invariants
 * are asserted in the stylesheets, as disabledControls.test does.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const stripComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, '')
const read = (rel: string) => stripComments(readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8'))

const PROC = read('./ProcurementPanel.css')
const HK = read('./HousekeepingPanel.css')
const WF = read('./WorkforcePanel.css')
const BIO = read('./BiomedicalPanel.css')
const DIET = read('./DietaryPanel.css')
const NABH = read('./NabhPanel.css')
const FD = read('./FrontDeskPanel.css')
const PAYER = read('./PayerPanel.css')
const PAY = read('./PaymentsPanel.css')
const RELAY = read('../Relay.css')
const PATIENT = read('../PatientShell.css')
const CLOCK = read('../../components/ClockWidget.css')
const CAPSIM = read('../../components/capacity/CapacitySimulator.css')

/** Splits a selector list on top-level commas only, so `:not(.a, .b)` stays whole. */
function splitSelectors(list: string): string[] {
  const out: string[] = []
  let depth = 0
  let cur = ''
  for (const ch of list) {
    if (ch === '(') depth++
    if (ch === ')') depth--
    if (ch === ',' && depth === 0) { out.push(cur.trim()); cur = ''; continue }
    cur += ch
  }
  out.push(cur.trim())
  return out.map(s => s.replace(/\s+/g, ' '))
}

/** Every innermost rule as [selectors, body]; @media wrappers are looked through. */
function rules(css: string): [string[], string][] {
  const out: [string[], string][] = []
  const re = /([^{}]+)\{([^{}]*)\}/g
  let m: RegExpExecArray | null
  while ((m = re.exec(css))) out.push([splitSelectors(m[1].trim()), m[2]])
  return out
}

/** The declaration block of the first rule whose selector list includes `selector`. */
function ruleFor(css: string, selector: string): string | null {
  return rules(css).find(([sels]) => sels.includes(selector))?.[1] ?? null
}

/** CSS specificity as [ids, classes, types]; :not() counts as its argument. */
function specificity(sel: string): [number, number, number] {
  let s = sel.replace(/:not\(([^)]*)\)/g, ' $1 ')
  const ids = (s.match(/#[\w-]+/g) || []).length
  const classes = (s.match(/\.[\w-]+|\[[^\]]*\]|:(?!:)[\w-]+/g) || []).length
  s = s.replace(/#[\w-]+|\.[\w-]+|\[[^\]]*\]|::?[\w-]+/g, ' ')
  const types = (s.match(/[a-z][\w-]*/gi) || []).length
  return [ids, classes, types]
}

const outranks = (a: string, b: string) => {
  const [x, y] = [specificity(a), specificity(b)]
  for (let i = 0; i < 3; i++) if (x[i] !== y[i]) return x[i] > y[i]
  return false
}

function cssFiles(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) cssFiles(p, out)
    else if (p.endsWith('.css')) out.push(p)
  }
  return out
}

describe('the no-opacity gate', () => {
  it('no stylesheet in src expresses a disabled control with opacity', () => {
    const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '../..') + '/'
    const offenders: string[] = []
    for (const file of cssFiles(SRC)) {
      for (const [sels, body] of rules(stripComments(readFileSync(file, 'utf8')))) {
        if (sels.some(s => /disabled/i.test(s)) && /(^|[;\s])opacity\s*:/.test(body)) {
          offenders.push(`${file.slice(SRC.length)}: ${sels.join(', ')}`)
        }
      }
    }
    expect(offenders).toEqual([])
  })
})

describe.each([
  ['Procurement', PROC, '.proc-btn:disabled'],
  ['Housekeeping', HK, '.hk-btn:disabled'],
  ['Workforce', WF, '.wf-btn:disabled'],
  ['Biomedical', BIO, '.bio-btn:disabled'],
  ['Dietary', DIET, '.diet-btn:disabled'],
  ['NABH', NABH, '.nabh-button:disabled'],
])('%s disabled button', (_name, css, selector) => {
  it('swaps to the neutral disabled pair with a not-allowed cursor, never opacity', () => {
    const body = ruleFor(css, selector)
    expect(body).not.toBeNull()
    expect(body).toMatch(/background:\s*var\(--btn-disabled-bg\)/)
    expect(body).toMatch(/color:\s*var\(--btn-disabled-text\)/)
    expect(body).toMatch(/border-color:\s*var\(--btn-disabled-border\)/)
    expect(body).toMatch(/cursor:\s*not-allowed/)
    expect(body).not.toMatch(/opacity/)
  })
})

describe('a filled button keeps its fill on hover', () => {
  it.each([
    ['Procurement', PROC, '.proc-btn:hover:not(:disabled)', '.proc-btn:not(.proc-btn--primary, .proc-btn--danger):hover:not(:disabled)'],
    ['Dietary', DIET, '.diet-btn:hover:not(:disabled)', '.diet-btn:not(.diet-btn--primary):hover:not(:disabled)'],
    ['NABH', NABH, '.nabh-button:hover', '.nabh-button:not(.nabh-button--primary, .nabh-button--danger):hover:not(:disabled)'],
  ])('%s: the glass hover skips the filled variants', (_name, css, bare, scoped) => {
    expect(ruleFor(css, bare)).toBeNull()
    expect(ruleFor(css, scoped)).toMatch(/background:\s*var\(--bg-glass-hover\)/)
  })

  it.each([
    ['Procurement', PROC, '.proc-btn--primary:hover:not(:disabled)'],
    ['Dietary', DIET, '.diet-btn--primary:hover:not(:disabled)'],
  ])('%s: the primary hovers to the paired accent', (_name, css, selector) => {
    expect(ruleFor(css, selector)).toMatch(/background:\s*var\(--btn-gradient-hover\)/)
  })
})

describe('animated state never fades', () => {
  it('the clock colon is static', () => {
    expect(ruleFor(CLOCK, '.clock-widget__seconds-dot')).not.toMatch(/animation/)
    expect(CLOCK).not.toMatch(/opacity/)
  })

  it('the capacity tiles draw their status stripe and busy dot at full strength', () => {
    expect(CAPSIM).not.toMatch(/opacity/)
  })

  it('a capacity tile with no result yet has a neutral stripe, not the accent', () => {
    // Unfaded, the accent is the danger red's twin in dark and the warn amber's in night.
    const stripe = ruleFor(CAPSIM, '.capsim__stat::before')
    expect(stripe).toMatch(/background:\s*var\(--text-muted\)/)
    expect(stripe).not.toMatch(/accent/)
  })

  it('the Relay alert dot pulses a ring, not its own opacity', () => {
    const dot = ruleFor(RELAY, '.relay-channel-item__alert-dot')
    expect(dot).not.toMatch(/pulse-soft/)
    expect(dot).toMatch(/animation:\s*relay-alert-ring/)
    const ring = RELAY.match(/@keyframes relay-alert-ring\s*\{([\s\S]*?)\n\}/)
    expect(ring?.[1]).toMatch(/outline-width/)
    expect(ring?.[1]).not.toMatch(/opacity/)
  })

  it('the report skeleton pulses between surface tokens', () => {
    const frames = PATIENT.match(/@keyframes bridge-skeleton\s*\{([\s\S]*?)\n\}/)
    expect(frames?.[1]).toMatch(/background:\s*var\(--bg-glass-hover\)/)
    expect(frames?.[1]).not.toMatch(/opacity/)
  })
})

describe('inline component styles never fade state', () => {
  // Block comments (JSX ones included) go with read(); line comments here.
  const code = (rel: string) => read(rel).replace(/(^|[^:'"])\/\/.*$/gm, '$1')

  it.each([
    ['Lens upload icon', '../../pages/panels/LensPanel.tsx'],
    ['Helix pharmacist notes', '../../components/helix/HelixOrderReviewModal.tsx'],
    ['Care bundle done step', '../../components/vigil/CareBundleTimer.tsx'],
  ])('%s', (_name, rel) => {
    expect(code(rel)).not.toMatch(/\bopacity\s*:/)
  })

  it('a done bundle step is struck through and quieted by token', () => {
    const src = code('../../components/vigil/CareBundleTimer.tsx')
    expect(src).toMatch(/color:\s*done\s*\?\s*TEXT_SECONDARY/)
    expect(src).toMatch(/textDecoration:\s*done\s*\?\s*'line-through'/)
  })
})

describe('hovering the selected tab keeps it selected', () => {
  it.each([
    ['Front desk', FD, 'fd-tab'],
    ['Payer', PAYER, 'payer-tab'],
    ['Payments', PAY, 'pay__tab'],
  ])('%s', (_name, css, tab) => {
    expect(ruleFor(css, `.${tab}:hover`)).toBeNull()
    const scoped = new RegExp(`@media \\(hover: hover\\)\\s*\\{\\s*\\.${tab}:not\\(\\.${tab}--active\\):hover\\s*\\{`)
    expect(css).toMatch(scoped)
  })
})

describe('numeric and readiness classes win over the table cell rule', () => {
  it.each([
    ['Payments amount cell', PAY, '.pay-table .pay-td--num', '.pay-table td'],
    ['Payments amount header', PAY, '.pay-table .pay-th--num', '.pay-table th'],
    ['Workforce number', WF, '.wf-table .wf-num', '.wf-table td'],
    ['Procurement number', PROC, '.proc-table .proc-num', '.proc-table td'],
    ['Housekeeping number', HK, '.hk-table .hk-num', '.hk-table tbody td'],
  ])('%s is right-aligned by specificity, not !important', (_name, css, utility, cell) => {
    expect(outranks(utility, cell)).toBe(true)
    const body = ruleFor(css, utility)
    expect(body).toMatch(/text-align:\s*right\s*;/)
    expect(body).not.toMatch(/!important/)
  })

  it.each([
    ['.hk-table .hk-ok', 'status-ok-text'],
    ['.hk-table .hk-warn', 'status-warn-text'],
    ['.hk-table .hk-danger', 'status-danger-text'],
    ['.hk-table .hk-late', 'status-danger-text'],
  ])('Housekeeping %s colours the cell', (utility, token) => {
    expect(outranks(utility, '.hk-table tbody td')).toBe(true)
    expect(ruleFor(HK, utility)).toMatch(new RegExp(`color:\\s*var\\(--${token}\\)`))
  })
})
