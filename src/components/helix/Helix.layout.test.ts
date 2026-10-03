/**
 * Helix layout, pinned in the stylesheet (jsdom computes no layout).
 *
 * Each table used to shrink into its own height-capped scroll box inside the
 * page scroller, so at 1440x900 the Fulfillment Pipeline showed its header and
 * no rows. On a phone the six stat cards stacked one per row and pushed the
 * work queue a full screen down.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const css = readFileSync(resolve(process.cwd(), 'src/components/helix/Helix.module.css'), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')

function media(query: string): string {
  const at = css.indexOf(`@media ${query} {`)
  if (at < 0) return ''
  let depth = 0
  for (let i = css.indexOf('{', at); i < css.length; i++) {
    if (css[i] === '{') depth++
    else if (css[i] === '}' && --depth === 0) return css.slice(css.indexOf('{', at) + 1, i)
  }
  return ''
}

function rule(text: string, selector: string): string {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const re = new RegExp(`(?:^|[}\\s])${esc}\\s*\\{([^{}]*)\\}`, 'g')
  return [...text.matchAll(re)].map(m => m[1]).join(';')
}

describe('Helix layout', () => {
  it('tables keep their height: one page scroller, no capped inner box', () => {
    expect(rule(css, '.tableContainer')).toMatch(/flex:\s*none/)
    const scroll = rule(css, '.tableScrollArea')
    expect(scroll).not.toMatch(/max-height/)
    expect(scroll).not.toMatch(/overflow-y/)
  })

  it('shows the stat cards two to a row on a phone', () => {
    expect(rule(media('(max-width: 600px)'), '.bentoGrid')).toMatch(/grid-template-columns:\s*repeat\(2/)
  })

  it('stacks the verification queue as cards under 900px', () => {
    const narrow = media('(max-width: 900px)')
    expect(narrow).toMatch(/\.verifyTable td\[data-label\]::before/)
    expect(narrow).not.toBe('')
  })

  it('gives every Helix control the 44px floor under a coarse pointer', () => {
    const coarse = media('(pointer: coarse)')
    for (const cls of ['.tab', '.btnPrimary', '.btnDanger', '.btnSecondary', '.verifyInput']) {
      expect(coarse, cls).toContain(cls)
    }
    expect(coarse).toMatch(/min-height:\s*var\(--tap-min\)/)
  })

  it('sizes a modal status badge to its label', () => {
    expect(rule(css, '.modalSection > .badge')).toMatch(/align-self:\s*flex-start/)
  })
})
