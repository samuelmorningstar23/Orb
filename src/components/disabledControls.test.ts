/**
 * DISABLED MUST LOOK DISABLED.
 *
 * The global :disabled rule in index.css is zero-specificity on purpose, so any
 * component class that sets its own fill or cursor beats it. Three surfaces did:
 * the Scribe mics kept their terracotta fill and pointer on an appliance with no
 * speech service, Helix's secondary and danger buttons had no disabled rule at
 * all, and Sage's night-theme Send label outranked the disabled pair (1.19:1).
 *
 * Vitest runs with css: false and jsdom computes no cascade, so the invariant is
 * asserted in the stylesheets, the same way ClinicianShell.primitives.test does.
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const read = (rel: string) =>
  readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '')

const SCRIBE = read('../pages/panels/ScribePanel.css')
const HELIX = read('./helix/Helix.module.css')
const SAGE = read('./sage/Sage.css')

/** The declaration block of the first rule whose selector list includes `selector`. */
function ruleFor(css: string, selector: string): string | null {
  const re = /([^{}]+)\{([^{}]*)\}/g
  let m: RegExpExecArray | null
  while ((m = re.exec(css))) {
    const selectors = m[1].split(',').map(s => s.trim())
    if (selectors.includes(selector)) return m[2]
  }
  return null
}

describe.each([
  ['Scribe record mic', SCRIBE, '.scribe-record:disabled'],
  ['Scribe live-stream mic', SCRIBE, '.scribe-stream-btn:disabled'],
  ['Helix secondary button', HELIX, '.btnSecondary:disabled'],
  ['Helix danger button', HELIX, '.btnDanger:disabled'],
])('%s', (_name, css, selector) => {
  const body = ruleFor(css, selector)

  it('has its own disabled rule', () => {
    expect(body).not.toBeNull()
  })

  it('uses the neutral disabled pair and a not-allowed cursor, never opacity', () => {
    expect(body).toMatch(/background:\s*var\(--btn-disabled-bg\)/)
    expect(body).toMatch(/color:\s*var\(--btn-disabled-text\)/)
    expect(body).toMatch(/cursor:\s*not-allowed/)
    expect(body).not.toMatch(/opacity/)
  })
})

describe('Scribe mics stop pulsing when disabled', () => {
  it.each(['.scribe-record:disabled', '.scribe-stream-btn:disabled'])('%s', selector => {
    expect(ruleFor(SCRIBE, selector)).toMatch(/animation:\s*none/)
  })
})

describe('Sage night theme', () => {
  it('gives only an ENABLED Send its dark ink label', () => {
    expect(SAGE).toMatch(/\[data-theme="night"\]\s*\.sage-btn-send:not\(:disabled\)\s*\{/)
    expect(SAGE).not.toMatch(/\[data-theme="night"\]\s*\.sage-btn-send\s*\{/)
  })
})
