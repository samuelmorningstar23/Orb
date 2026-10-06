/**
 * Source checks for two habits that kept coming back:
 *
 *  - Emoji used as icons. They render differently on every tablet, cannot take
 *    the theme colour, and are read aloud by screen readers. Icons come from
 *    components/common/Icon.tsx. Files that still carry emoji are listed in
 *    EMOJI_RATCHET with their current count; the count may only go down, and
 *    no other file may add one.
 *  - Native browser dialogs (alert, prompt, confirm). They block the whole
 *    screen, name no patient, and the message is gone once dismissed. Errors
 *    and reasons are shown inline beside the control instead.
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const SRC = dirname(dirname(fileURLToPath(import.meta.url)))

// Colour emoji: anything that presents as emoji by default, or a pictograph
// forced to emoji presentation with VS16. Text glyphs such as ✓ → ↻ pass.
const EMOJI_RE = /\p{Emoji_Presentation}|\p{Extended_Pictographic}\uFE0F/gu
const DIALOG_RE = /(?<![\w.])(?:window\.)?(alert|prompt|confirm)\s*\(/g

/** Blank out comments so a note about an old emoji or a removed alert() does
 *  not count. Line structure is kept so reported line numbers stay right. */
function stripComments(source: string): string {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, m => m.replace(/[^\n]/g, ' '))
    .replace(/(^|[^:'"`\\])\/\/[^\n]*/g, (_m, lead: string) => lead)
}

function findEmoji(source: string): { line: number; glyph: string }[] {
  const out: { line: number; glyph: string }[] = []
  stripComments(source).split('\n').forEach((text, i) => {
    for (const m of text.matchAll(EMOJI_RE)) out.push({ line: i + 1, glyph: m[0] })
  })
  return out
}

function findNativeDialogs(source: string): { line: number; call: string }[] {
  const out: { line: number; call: string }[] = []
  stripComments(source).split('\n').forEach((text, i) => {
    for (const m of text.matchAll(DIALOG_RE)) out.push({ line: i + 1, call: m[1] })
  })
  return out
}

/** Files that still hold emoji (owned by other work), with today's count. */
const EMOJI_RATCHET: Record<string, number> = {
  'api/thinkingMode.ts': 3,
  'components/relay/AlertsSection.tsx': 1,
  'components/sage/SageComposer.tsx': 1,
  'components/sage/SageInsightsFeed.tsx': 1,
  'pages/PatientShell.tsx': 5,
  'pages/panels/LensPanel.tsx': 1,
  'pages/panels/ScribePanel.tsx': 26,
}

function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) {
      if (name !== 'test') sourceFiles(path, out)
    } else if (/\.tsx?$/.test(name) && !/\.(test|spec)\.tsx?$/.test(name) && !name.endsWith('.d.ts')) {
      out.push(path)
    }
  }
  return out
}

describe('the checks themselves', () => {
  it('flags an emoji used as an icon in JSX', () => {
    const fixture = [
      'export function Bar() {',
      '  return <button><span>💊</span> Medications</button>',
      '}',
    ].join('\n')
    expect(findEmoji(fixture)).toEqual([{ line: 2, glyph: '💊' }])
  })

  it('flags a VS16 pictograph but not text glyphs or comments', () => {
    const fixture = [
      "const a = '⚠️ CRITICAL'",
      "const b = '✓ saved → next ↻'",
      '// the old 🩻 icon',
      '{/* 🔬 was here */}',
    ].join('\n')
    expect(findEmoji(fixture)).toEqual([{ line: 1, glyph: '⚠️' }])
  })

  it('flags native dialogs but not methods or comments', () => {
    const fixture = [
      "alert('x')",
      "const t = window.prompt('Result')",
      'if (confirm("Sure?")) go()',
      'toast.alert(x); sendPrompt(y); form.confirm()',
      '// alert() blocked the screen',
    ].join('\n')
    expect(findNativeDialogs(fixture).map(d => d.call)).toEqual(['alert', 'prompt', 'confirm'])
  })
})

describe('the app source', () => {
  const files = sourceFiles(SRC)

  it('adds no emoji icons, and the listed files only lose them', () => {
    const problems: string[] = []
    for (const file of files) {
      const rel = relative(SRC, file).split('\\').join('/')
      const found = findEmoji(readFileSync(file, 'utf8'))
      const allowed = EMOJI_RATCHET[rel] ?? 0
      if (found.length > allowed) {
        problems.push(`${rel}: ${found.length} emoji (allowed ${allowed}), e.g. line ${found[0].line} ${found[0].glyph}`)
      }
    }
    expect(problems).toEqual([])
  })

  it('opens no native alert, prompt or confirm dialog', () => {
    const problems: string[] = []
    for (const file of files) {
      const rel = relative(SRC, file).split('\\').join('/')
      for (const d of findNativeDialogs(readFileSync(file, 'utf8'))) {
        problems.push(`${rel}:${d.line} ${d.call}()`)
      }
    }
    expect(problems).toEqual([])
  })
})
