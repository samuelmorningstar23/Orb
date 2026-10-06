/**
 * Code 128 barcodes for specimen labels (lis-8, ruling LIS-R15: browser
 * printing to thermal label printers, read back by keyboard-mode scanners).
 *
 * Pure and dependency-free: text in, bar widths out. The label view draws the
 * widths as SVG rectangles, so nothing here touches the DOM.
 *
 * SUBSETS. An accession number is 12 digits (ruling LIS-R13), which subset C
 * packs two digits to a symbol: 6 symbols instead of 12, so the bars stay wide
 * enough for a 203 dpi printer on a 50 mm label. A site prefix (letters) is
 * written in subset B, and a run of four or more digits after it switches to
 * subset C for the even part of the run. Characters outside printable ASCII
 * (32 to 126) are refused rather than guessed.
 *
 * CHECKSUM. The start symbol's value, plus each data symbol's value times its
 * position (1, 2, 3 ...), modulo 103.
 *
 * Widths are module counts, bar first, alternating bar and space: six per
 * symbol (three bars, three spaces, eleven modules), seven for the stop
 * (thirteen modules, ending on a bar).
 */

/** The 107 Code 128 symbols as bar and space widths, by value. 103, 104 and
 *  105 are Start A, B and C; 106 is Stop. */
export const CODE128_PATTERNS: readonly string[] = [
  '212222', '222122', '222221', '121223', '121322', '131222', '122213', '122312', '132212', '221213',
  '221312', '231212', '112232', '122132', '122231', '113222', '123122', '123221', '223211', '221132',
  '221231', '213212', '223112', '312131', '311222', '321122', '321221', '312212', '322112', '322211',
  '212123', '212321', '232121', '111323', '131123', '131321', '112313', '132113', '132311', '211313',
  '231113', '231311', '112133', '112331', '132131', '113123', '113321', '133121', '313121', '211331',
  '231131', '213113', '213311', '213131', '311123', '311321', '331121', '312113', '312311', '332111',
  '314111', '221411', '431111', '111224', '111422', '121124', '121421', '141122', '141221', '112214',
  '112412', '122114', '122411', '142112', '142211', '241211', '221114', '413111', '241112', '134111',
  '111242', '121142', '121241', '114212', '124112', '124211', '411212', '421112', '421211', '212141',
  '214121', '412121', '111143', '111341', '131141', '114113', '114311', '411113', '411311', '113141',
  '114131', '311141', '411131', '211412', '211214', '211232', '2331112',
]

export const START_B = 104
export const START_C = 105
export const STOP = 106
/** In subset B: switch to subset C. */
export const CODE_C = 99
/** In subset C: switch to subset B. */
export const CODE_B = 100

/** Quiet zone each side, in modules (the standard asks for at least ten). */
export const QUIET_ZONE_MODULES = 10

export type Code128Subset = 'B' | 'C'

export interface Code128Encoding {
  /** The text encoded. */
  text: string
  /** The subset the symbol starts in. */
  startSubset: Code128Subset
  /** Every symbol value in order: start, data (with any subset switches),
   *  checksum, stop. */
  codes: number[]
  /** The checksum symbol's value. */
  checksum: number
  /** Bar and space widths in modules, bar first, quiet zones excluded. */
  widths: number[]
  /** Total modules, quiet zones excluded. */
  modules: number
}

/** One bar, in modules from the left edge of the quiet zone. */
export interface Code128Bar {
  x: number
  width: number
}

export class Code128Error extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'Code128Error'
  }
}

const isDigit = (ch: string) => ch >= '0' && ch <= '9'

function digitRun(text: string, from: number): number {
  let end = from
  while (end < text.length && isDigit(text[end])) end += 1
  return end - from
}

function subsetBValue(ch: string): number {
  const code = ch.charCodeAt(0)
  if (code < 32 || code > 126) {
    throw new Code128Error(`Code 128 subset B cannot encode the character code ${code}.`)
  }
  return code - 32
}

/**
 * Split the text into subset segments. All digits, even length: subset C
 * throughout. Otherwise subset B, switching to C for the even part of any run
 * of four or more digits (an odd leading digit stays in B).
 */
function segments(text: string): { subset: Code128Subset; text: string }[] {
  if (text.length > 0 && text.length % 2 === 0 && digitRun(text, 0) === text.length) {
    return [{ subset: 'C', text }]
  }
  const out: { subset: Code128Subset; text: string }[] = []
  const push = (subset: Code128Subset, part: string) => {
    if (!part) return
    const last = out[out.length - 1]
    if (last && last.subset === subset) last.text += part
    else out.push({ subset, text: part })
  }
  let i = 0
  while (i < text.length) {
    const run = digitRun(text, i)
    if (run >= 4) {
      const odd = run % 2
      push('B', text.slice(i, i + odd))
      push('C', text.slice(i + odd, i + run))
      i += run
    } else if (run > 0) {
      push('B', text.slice(i, i + run))
      i += run
    } else {
      push('B', text[i])
      i += 1
    }
  }
  return out
}

/** Encode `text` as Code 128. Throws Code128Error for empty text or a
 *  character subset B cannot carry. */
export function encodeCode128(text: string): Code128Encoding {
  if (typeof text !== 'string' || text.length === 0) {
    throw new Code128Error('Nothing to encode.')
  }
  const parts = segments(text)
  const startSubset = parts[0].subset
  const codes: number[] = [startSubset === 'C' ? START_C : START_B]
  let current = startSubset
  for (const part of parts) {
    if (part.subset !== current) {
      codes.push(part.subset === 'C' ? CODE_C : CODE_B)
      current = part.subset
    }
    if (part.subset === 'C') {
      for (let k = 0; k < part.text.length; k += 2) codes.push(Number(part.text.slice(k, k + 2)))
    } else {
      for (const ch of part.text) codes.push(subsetBValue(ch))
    }
  }
  let sum = codes[0]
  for (let position = 1; position < codes.length; position += 1) {
    sum += codes[position] * position
  }
  const checksum = sum % 103
  codes.push(checksum, STOP)
  const widths = codes.flatMap(code => CODE128_PATTERNS[code].split('').map(Number))
  return {
    text,
    startSubset,
    codes,
    checksum,
    widths,
    modules: widths.reduce((a, b) => a + b, 0),
  }
}

/** The bars of an encoding as rectangles, offset by the left quiet zone. */
export function code128Bars(encoding: Code128Encoding): Code128Bar[] {
  const bars: Code128Bar[] = []
  let x = QUIET_ZONE_MODULES
  encoding.widths.forEach((width, index) => {
    if (index % 2 === 0) bars.push({ x, width })
    x += width
  })
  return bars
}

/** Total width in modules, both quiet zones included. */
export function code128TotalModules(encoding: Code128Encoding): number {
  return encoding.modules + 2 * QUIET_ZONE_MODULES
}

/** The bars and spaces as a '1'/'0' module string, for tests and checks. */
export function code128ModuleString(encoding: Code128Encoding): string {
  return encoding.widths.map((width, index) => (index % 2 === 0 ? '1' : '0').repeat(width)).join('')
}
