/**
 * Laboratory values as the bench types them (lis-10).
 *
 * The server decides every result: it parses the value again at the test's
 * reporting precision, refuses what the test cannot take and decides the
 * flag against an approved range. This file only lets the entry grid say
 * what is wrong BEFORE a value is sent, in the same words, and print a value
 * at its precision. It never decides a flag.
 *
 * THE RULES, mirrored from server/services/lab_result_service.parse_numeric:
 *   * A number at the test's reporting precision. More decimal places than
 *     the test reports is refused, never rounded ("4.56" for a one-place
 *     test). Fewer is the same number ("4.5" for a two-place test is 4.50).
 *     Trailing zeros beyond the precision are fine ("4.50" for one place).
 *   * A bound: "<", ">", "<=", ">=" (or the signs ≤ and ≥) before the number,
 *     as in "<0.01" or ">600". The bound is held to the same precision.
 *   * No exponent, no thousands separator, no words.
 *
 * Arithmetic is on the digits, never on a float, so 0.1 + 0.2 never enters a
 * laboratory value. A value is carried in thousandths (milli), as the server
 * stores it.
 */

export type LabComparator = '<' | '>' | '<=' | '>='

export interface LabNumber {
  ok: true
  comparator: LabComparator | null
  /** Thousandths of the value (the bound, for a comparator result). */
  milli: number
  /** As the report prints it: "4.50", "<0.01". */
  text: string
}

export interface LabValueProblem {
  ok: false
  code: 'value_required' | 'not_a_number' | 'precision' | 'out_of_range'
  message: string
}

/** Thousandths. Far beyond any laboratory value and still an exact integer. */
const MILLI_LIMIT = 1e15

const COMPARATOR = /^(<=|>=|<|>|≤|≥)\s*(.*)$/
const NUMBER = /^([+-]?)(\d*)(?:\.(\d*))?$/

function placesWords(places: number): string {
  if (places === 0) return 'whole numbers'
  return `${places} decimal place${places === 1 ? '' : 's'}`
}

/** How a person reads "reported to N places". */
export function precisionMessage(testName: string, decimalPlaces: number): string {
  return `${testName} is reported in ${placesWords(decimalPlaces)}. Type the value as the report prints it; Orb does not round a result.`
}

/** Thousandths at a reporting precision, trailing zeros kept: (4500, 2) is "4.50". */
export function formatMilli(milli: number | null | undefined, decimalPlaces: number): string {
  if (milli === null || milli === undefined || !Number.isFinite(milli)) return ''
  const places = Math.max(0, Math.min(3, Math.trunc(decimalPlaces)))
  const negative = milli < 0
  const abs = Math.abs(Math.trunc(milli))
  const whole = Math.floor(abs / 1000)
  const fraction = String(abs % 1000).padStart(3, '0').slice(0, places)
  const body = places > 0 ? `${whole}.${fraction}` : String(whole)
  const isZero = abs === 0 || /^0(?:\.0*)?$/.test(body)
  return negative && !isZero ? `-${body}` : body
}

/**
 * A typed numeric result, checked at the test's reporting precision.
 * `testName` is used in the precision message.
 */
export function parseLabNumber(
  input: string,
  decimalPlaces: number,
  testName = 'This test',
): LabNumber | LabValueProblem {
  let raw = (input ?? '').trim()
  if (!raw) return { ok: false, code: 'value_required', message: 'Type the result.' }
  let comparator: LabComparator | null = null
  const bound = COMPARATOR.exec(raw)
  if (bound) {
    const sign = bound[1]
    comparator = sign === '≤' ? '<=' : sign === '≥' ? '>=' : (sign as LabComparator)
    raw = bound[2].trim()
  }
  const match = NUMBER.exec(raw)
  if (!match || (match[2] === '' && (match[3] ?? '') === '')) {
    return {
      ok: false,
      code: 'not_a_number',
      message: 'Type a number, such as 4.2, or a bound such as <0.01 or >600.',
    }
  }
  const [, sign, wholeDigits, fractionDigits = ''] = match
  const places = Math.max(0, Math.min(3, Math.trunc(decimalPlaces)))
  const significant = fractionDigits.replace(/0+$/, '')
  if (significant.length > places) {
    return { ok: false, code: 'precision', message: precisionMessage(testName, places) }
  }
  const whole = Number(wholeDigits || '0')
  const fraction = Number(significant.padEnd(3, '0') || '0')
  const magnitude = whole * 1000 + fraction
  if (!Number.isSafeInteger(magnitude) || magnitude >= MILLI_LIMIT) {
    return { ok: false, code: 'out_of_range', message: 'This value is out of range.' }
  }
  const milli = sign === '-' ? -magnitude : magnitude
  const shown = formatMilli(milli, places)
  return { ok: true, comparator, milli, text: comparator ? `${comparator}${shown}` : shown }
}

/**
 * One of a coded test's values, matched without regard to case or spacing,
 * returned in the catalogue's spelling; null when it is not on the list.
 */
export function matchCodedValue(input: string, allowed: readonly string[]): string | null {
  const typed = (input ?? '').replace(/\s+/g, ' ').trim().toLowerCase()
  if (!typed) return null
  return allowed.find(value => value.toLowerCase() === typed) ?? null
}

/** A stored flag in words, for small text. Blank for no flag. */
export function flagWords(flag: string | null | undefined): string {
  switch (flag) {
    case 'H': return 'High'
    case 'L': return 'Low'
    case 'HH': return 'Critically high'
    case 'LL': return 'Critically low'
    case 'A': return 'Abnormal'
    default: return ''
  }
}
