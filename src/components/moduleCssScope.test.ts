/**
 * A CSS MODULE MAY NOT STYLE A BARE ELEMENT.
 *
 * CSS Modules hashes class names and nothing else. A selector with no local
 * class in it (`button`, `button:disabled`, `table td`) ships as a GLOBAL rule
 * the moment the module's chunk loads, and stays for the session.
 * EmergencyIntakePopup.module.css carried `button` and `button:disabled`, so
 * opening Surgical Suite once repainted every button in the app.
 *
 * Vitest runs with css: false, so this is asserted in the files: every
 * selector in every *.module.css must name a local class.
 */
import { readFileSync, readdirSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const SRC = dirname(dirname(fileURLToPath(import.meta.url)))

const MODULES = (readdirSync(SRC, { recursive: true }) as string[])
  .filter(p => p.endsWith('.module.css'))
  .map(p => join(SRC, p))

/** Every style-rule selector in `css`, with @media/@supports bodies walked
 *  into and @keyframes bodies (from, to, 50%) skipped. */
function selectorsOf(css: string): string[] {
  const text = css.replace(/\/\*[\s\S]*?\*\//g, '')
  const out: string[] = []
  const walk = (body: string) => {
    let i = 0
    while (i < body.length) {
      const open = body.indexOf('{', i)
      if (open < 0) return
      const prelude = body.slice(i, open).replace(/^[\s\S]*;/, '').trim()
      let depth = 1
      let j = open + 1
      while (j < body.length && depth > 0) {
        if (body[j] === '{') depth++
        else if (body[j] === '}') depth--
        j++
      }
      const inner = body.slice(open + 1, j - 1)
      if (prelude.startsWith('@')) {
        if (!/^@(-webkit-)?keyframes\b/.test(prelude) && !/^@font-face\b/.test(prelude)) walk(inner)
      } else if (prelude) {
        out.push(...prelude.split(',').map(s => s.trim()).filter(Boolean))
      }
      i = j
    }
  }
  walk(text)
  return out
}

/** True when the selector is anchored to a class this module owns. Classes
 *  inside :global() are not local, and :not(.x) narrows nothing: `button:not(.x)`
 *  still matches every other button in the app. */
function namesLocalClass(selector: string): boolean {
  const stripped = selector
    .replace(/:global\([^)]*\)/g, '')
    .replace(/:not\([^)]*\)/g, '')
    .replace(/\[[^\]]*\]/g, '')
  return /\.[A-Za-z_-][\w-]*/.test(stripped)
}

describe('the gate itself', () => {
  it('flags bare element selectors and passes class-anchored ones', () => {
    const css = `
      button { padding: 0 }
      button:disabled, .ok:disabled { color: red }
      @media (pointer: coarse) { td { min-height: 44px } .row td { color: red } }
      @keyframes spin { from { opacity: 0 } to { opacity: 1 } }
      :global(.shell) a { color: red }
      a:not(.x) { color: red }
      :where(.popup) button { color: red }
    `
    const bad = selectorsOf(css).filter(s => !namesLocalClass(s))
    expect(bad).toEqual(['button', 'button:disabled', 'td', ':global(.shell) a', 'a:not(.x)'])
  })
})

describe('CSS modules name a local class in every selector', () => {
  it('finds the module stylesheets', () => {
    expect(MODULES.length).toBeGreaterThan(0)
  })

  it.each(MODULES.map(p => [relative(SRC, p), p]))('%s', (_name, path) => {
    const bare = selectorsOf(readFileSync(path, 'utf8')).filter(s => !namesLocalClass(s))
    expect(bare).toEqual([])
  })
})
