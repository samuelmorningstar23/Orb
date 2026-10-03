/**
 * A <td> stays a table cell (F092).
 *
 * Front Desk Transfers gave its MRN/Bed cells display:block and its Action cell
 * display:flex. A cell that is not display:table-cell leaves the table's column
 * grid, so the Action column rendered empty and Bed and Action shared one; in
 * Equipment, Procurement and Diet the same pattern broke the row hairlines.
 * The flex row now lives on a div inside the cell.
 *
 * jsdom computes no layout and vitest runs with css: false, so this is asserted
 * in the files: every class a panel puts on a <td> must not be the subject of a
 * rule that sets display to anything but table-cell.
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const read = (rel: string) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')
const stripComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, '')

const PANELS = ['FrontDeskPanel', 'BiomedicalPanel', 'ProcurementPanel', 'DietaryPanel']

/** Static class names used on <td> elements, including the literal parts of a
 *  template-literal className. */
function tdClasses(tsx: string): Set<string> {
  const out = new Set<string>()
  const re = /<td\b[^>]*?className=(?:"([^"]+)"|\{`([^`]+)`\})/g
  for (const m of tsx.matchAll(re)) {
    const raw = (m[1] ?? m[2] ?? '').replace(/\$\{[^}]*\}/g, ' ')
    for (const cls of raw.split(/\s+/)) if (cls) out.add(cls)
  }
  return out
}

/** Every `display` value set by a rule whose SUBJECT (last compound selector)
 *  carries `cls`. `.x > * + *` styles the children, not the cell, and is fine. */
function displaysFor(css: string, cls: string): string[] {
  const found: string[] = []
  const escaped = cls.replace(/[.*+?^${}()|[\]\\-]/g, '\\$&')
  const onSubject = new RegExp(`\\.${escaped}(?![\\w-])`)
  for (const m of stripComments(css).matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const subjects = m[1].split(',').map(sel => sel.trim().split(/\s*[\s>+~]\s*/).pop() ?? '')
    if (!subjects.some(s => onSubject.test(s))) continue
    for (const d of m[2].matchAll(/(?:^|;)\s*display\s*:\s*([^;]+)/g)) found.push(d[1].trim())
  }
  return found
}

describe('table cells keep display: table-cell', () => {
  for (const panel of PANELS) {
    it(`${panel}: no <td> class sets another display`, () => {
      const tsx = read(`./${panel}.tsx`)
      const css = read(`./${panel}.css`)
      const classes = tdClasses(tsx)
      expect(classes.size).toBeGreaterThan(0)
      const offenders = [...classes]
        .map(cls => ({ cls, displays: displaysFor(css, cls).filter(d => d !== 'table-cell') }))
        .filter(o => o.displays.length > 0)
      expect(offenders).toEqual([])
    })
  }

  it('the gate catches the original defect', () => {
    const css = '.fd-actions { display: flex; gap: 8px; }\n.fd-mono { display: block; }'
    expect(displaysFor(css, 'fd-actions')).toEqual(['flex'])
    expect(displaysFor(css, 'fd-mono')).toEqual(['block'])
    expect(displaysFor('.x > * + * { display: inline; }', 'x')).toEqual([])
  })
})
