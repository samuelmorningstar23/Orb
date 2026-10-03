/**
 * Surgical Suite layout, pinned in the stylesheet (jsdom computes no layout).
 *
 * The suite was sized as the viewport minus a 220px sidebar and 64px of
 * padding, so on a phone, where neither exists, it was about 106px wide. The
 * day footer was a space-between row whose three uppercase labels ran into
 * each other, and the phone block set grid columns on a flex strip.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const css = readFileSync(resolve(process.cwd(), 'src/pages/panels/SurgicalSuitePanel.css'), 'utf8')
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

describe('Surgical Suite layout', () => {
  it('takes its parent width, never a viewport sum that assumes a sidebar', () => {
    expect(css).not.toMatch(/100vw\s*-\s*220px/)
    const suite = rule(css, '.suite')
    expect(suite).toMatch(/width:\s*100%/)
    expect(suite).toMatch(/max-width:\s*100%/)
  })

  it('never sets grid columns on the week strip, which is a flex row', () => {
    expect(rule(media('(max-width: 900px)'), '.suite__week')).not.toMatch(/grid-template-columns/)
    expect(rule(media('(max-width: 600px)'), '.suite__week')).not.toMatch(/grid-template-columns/)
  })

  it('on a phone, a day card is narrower than the screen and only as tall as its cases', () => {
    const card = rule(media('(max-width: 600px)'), '.suite__day-card')
    expect(card).toMatch(/width:\s*min\(/)
    expect(card).toMatch(/min-height:\s*0/)
  })

  it('lays the day footer out as three columns with a gap', () => {
    const footer = rule(css, '.suite__day-footer')
    expect(footer).toMatch(/display:\s*grid/)
    expect(footer).toMatch(/grid-template-columns:\s*repeat\(3/)
    expect(footer).toMatch(/gap:/)
    expect(rule(css, '.suite__day-footer-lbl')).not.toMatch(/text-transform:\s*uppercase/)
  })
})
