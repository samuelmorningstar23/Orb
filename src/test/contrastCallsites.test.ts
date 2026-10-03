/**
 * The contrast gate's call-site scanner must MEASURE a chip tinted with
 * color-mix(), not skip it.
 *
 * Its property parser allowed one level of parentheses, so
 * `'color-mix(in srgb, var(--status-ok) 10%, transparent)'` was cut at the
 * comma inside the mix and the site was counted as unresolvable. Every status
 * chip written that way went unmeasured, which is how fill tokens kept being
 * used as small text (below 4.5:1) while the gate stayed green.
 */
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, describe, expect, it } from 'vitest'
import { measureSite, scanTsx } from '../../scripts/lib/contrast-callsites.mjs'

// Dark-theme values and surface from src/index.css.
const DARK = { '--status-danger': '#e8756a', '--status-danger-text': '#ee9c94' }
const DARK_SURFACE = [[0x2f, 0x2e, 0x2b]]

const dir = mkdtempSync(join(tmpdir(), 'orb-contrast-'))
afterAll(() => rmSync(dir, { recursive: true, force: true }))

function sitesFor(tsx: string) {
  const file = join(dir, `Fixture${Math.random().toString(36).slice(2)}.tsx`)
  writeFileSync(file, tsx)
  return scanTsx(file, dir)
}

describe('call-site scanner on a color-mix tint', () => {
  it('reads the whole color-mix value, nested parentheses included', () => {
    const [site] = sitesFor(
      "export const Chip = () => <span style={{ color: 'var(--status-ok)', background: 'color-mix(in srgb, var(--status-ok) 10%, transparent)', fontSize: '0.7rem' }}>On time</span>",
    )
    expect(site.colors.map(c => c.value)).toEqual(['var(--status-ok)'])
    expect(site.backgrounds.map(b => b.value)).toEqual(['color-mix(in srgb, var(--status-ok) 10%, transparent)'])
  })

  it('measures the site, failing the fill token and passing the text token', () => {
    const [fill] = sitesFor(
      "export const A = () => <span style={{ color: 'var(--status-danger)', background: 'color-mix(in srgb, var(--status-danger) 10%, transparent)', fontSize: '0.7rem' }}>Overdue</span>",
    )
    const [text] = sitesFor(
      "export const B = () => <span style={{ color: 'var(--status-danger-text)', background: 'color-mix(in srgb, var(--status-danger) 10%, transparent)', fontSize: '0.7rem' }}>Overdue</span>",
    )
    const bad = measureSite(fill, DARK, {}, DARK_SURFACE, 'dark')
    const good = measureSite(text, DARK, {}, DARK_SURFACE, 'dark')
    expect(bad).not.toBeNull()
    expect(good).not.toBeNull()
    expect(bad!.ratio).toBeLessThan(bad!.need)
    expect(good!.ratio).toBeGreaterThanOrEqual(good!.need)
  })

  it('reads both branches of a multi-line ternary', () => {
    const [site] = sitesFor([
      'export const C = ({ ok }: { ok: boolean }) => <span style={{',
      "  background: ok ? 'color-mix(in srgb, var(--status-ok) 10%, transparent)' : 'var(--bg-glass)',",
      '  color: ok',
      "    ? 'var(--status-ok-text)'",
      "    : 'var(--text-secondary)',",
      '}}>x</span>',
    ].join('\n'))
    expect(site.colors.map(c => c.value)).toEqual(['var(--status-ok-text)', 'var(--text-secondary)'])
    expect(site.backgrounds.map(b => b.value)).toEqual(['color-mix(in srgb, var(--status-ok) 10%, transparent)', 'var(--bg-glass)'])
  })
})
