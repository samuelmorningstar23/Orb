/**
 * The shell's layout contract, pinned in the stylesheets that carry it.
 *
 * jsdom computes no layout, so these failures cannot be watched in a render
 * test: hospital modules that could not scroll, header controls pushed off a
 * phone screen, nurse tabs printed over each other, the voice toggle swelling
 * into a second orb, and the orb sitting on the last row's actions. Each rule
 * below is the fix for one of them; deleting it turns this file red.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

function css(path: string): string {
  return readFileSync(resolve(process.cwd(), path), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '')
}

/** The body of the first `@media <query> {` block, braces balanced. */
function media(text: string, query: string, from = 0): string {
  const at = text.indexOf(`@media ${query} {`, from)
  if (at < 0) return ''
  let depth = 0
  for (let i = text.indexOf('{', at); i < text.length; i++) {
    if (text[i] === '{') depth++
    else if (text[i] === '}' && --depth === 0) return text.slice(text.indexOf('{', at) + 1, i)
  }
  return ''
}

/** Every declaration block for `selector`, joined. Top-level rules only when
 *  `text` is a whole file; pass a media() body to read inside a query. */
function rule(text: string, selector: string): string {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const re = new RegExp(`(?:^|[}\\s])${esc}\\s*\\{([^{}]*)\\}`, 'g')
  return [...text.matchAll(re)].map(m => m[1]).join(';')
}

const shell = css('src/pages/ClinicianShell.css')
const root = css('src/index.css')

describe('hospital modules scroll inside the shell', () => {
  it('the module wrapper is a scroller with the orb\'s corner reserved', () => {
    const body = rule(shell, '.shell__module-scroll')
    expect(body).toMatch(/overflow-y:\s*auto/)
    expect(body).toMatch(/min-height:\s*0/)
    expect(body).toMatch(/padding-bottom:\s*var\(--fab-clearance\)/)
  })

  it('the clearance is a root token', () => {
    expect(rule(root, ':root')).toMatch(/--fab-clearance:\s*\d+px/)
  })
})

describe('phone header', () => {
  it('the right-hand controls take their own line and wrap inside it', () => {
    const phone = media(shell, '(max-width: 768px)')
    const body = rule(phone, '.shell__header-right')
    expect(body).toMatch(/flex:\s*1 1 100%/)
    expect(body).toMatch(/min-width:\s*0/)
    expect(body).toMatch(/flex-wrap:\s*wrap/)
  })

  it('the language toggle meets the tap floor on a touch screen', () => {
    const coarse = media(shell, '(pointer: coarse)')
    expect(rule(coarse, '.shell__lang-toggle')).toMatch(/min-width:\s*var\(--tap-min\)/)
  })
})

describe('nurse view tabs', () => {
  const nurse = css('src/pages/panels/nurse/NursePanel.css')

  it('size to their label, not to the global 44px floor', () => {
    expect(rule(nurse, '.nurse-view-tab')).toMatch(/min-width:\s*auto/)
  })

  it('do not shrink under each other on a tablet or phone', () => {
    const body = rule(media(nurse, '(max-width: 768px)'), '.nurse-view-tab')
    expect(body).toMatch(/white-space:\s*nowrap/)
    expect(body).toMatch(/flex-shrink:\s*0/)
  })
})

describe('the floating Sage orb', () => {
  const fab = css('src/components/sage/SageFloatingAssistant.css')

  it('keeps the voice toggle a 24px dot under the coarse-pointer floor', () => {
    const body = rule(fab, '.sage-fab__toggle')
    expect(body).toMatch(/width:\s*24px/)
    expect(body).toMatch(/min-width:\s*0/)
    expect(body).toMatch(/min-height:\s*0/)
  })

  it('lifts clear of a bottom composer, and steps aside on a phone where the tab has its own mic', () => {
    expect(rule(fab, '.sage-fab--raised')).toMatch(/bottom:\s*\d+px/)
    expect(rule(media(fab, '(max-width: 768px)'), '.sage-fab--own-mic')).toMatch(/display:\s*none/)
  })

  it('the admin panel and the payer drawer leave its corner free', () => {
    const admin = rule(css('src/pages/panels/admin/Admin.css'), '.admin-panel')
    expect(admin).toMatch(/overflow-y:\s*auto/)
    expect(admin).toMatch(/padding-bottom:\s*var\(--fab-clearance\)/)
    expect(rule(css('src/pages/panels/PayerPanel.css'), '.payer-drawer'))
      .toMatch(/padding-bottom:\s*calc\([^)]*var\(--fab-clearance\)\)/)
  })
})
