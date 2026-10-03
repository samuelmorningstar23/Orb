/**
 * SAGE AT PHONE WIDTH: the composer keeps the screen's width.
 *
 * The 264px history rail sat beside the chat with flex-shrink 0, so at 390px the
 * chat column was about 100px, the textarea 0px wide and Send off-screen, for
 * every role that has Sage. At 768px and below the rail now stacks above the
 * chat, its list is capped, and the composer's controls wrap under a full-width
 * textarea. Measured in Chromium at 390x844 (mouse and touch): textarea 314px,
 * Send inside the viewport, no horizontal scroll.
 *
 * Vitest runs with css: false, so the rule is pinned in the stylesheet itself.
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

// Through a function: a literal new URL('./Sage.css', import.meta.url) is
// rewritten by Vite into a served asset URL, which readFileSync cannot open.
const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')
const CSS = read('./Sage.css').replace(/\/\*[\s\S]*?\*\//g, '')

/** The body of `@media (max-width: 768px) { ... }`, braces balanced. */
function phoneBlock(): string {
  const start = CSS.indexOf('@media (max-width: 768px)')
  expect(start).toBeGreaterThan(-1)
  const open = CSS.indexOf('{', start)
  let depth = 0
  for (let i = open; i < CSS.length; i++) {
    if (CSS[i] === '{') depth++
    else if (CSS[i] === '}' && --depth === 0) return CSS.slice(open + 1, i)
  }
  throw new Error('unbalanced @media block')
}

const rule = (block: string, selector: string) => {
  const m = new RegExp(`${selector.replace(/[.*+?^${}()|[\]\\-]/g, '\\$&')}\\s*\\{([^}]*)\\}`).exec(block)
  return m ? m[1] : ''
}

describe('Sage at phone width', () => {
  const block = phoneBlock()

  it('stacks the history rail above the chat', () => {
    expect(rule(block, '.sage-workspace')).toMatch(/flex-direction:\s*column/)
    expect(rule(block, '.sage-sidebar')).toMatch(/width:\s*100%/)
  })

  it('caps the history list so it cannot push the composer away', () => {
    expect(rule(block, '.sage-sidebar__list')).toMatch(/max-height:\s*140px/)
  })

  it('gives the textarea its own full-width row', () => {
    expect(rule(block, '.sage-composer')).toMatch(/flex-wrap:\s*wrap/)
    expect(rule(block, '.sage-composer__input')).toMatch(/flex:\s*1 1 100%/)
  })
})
