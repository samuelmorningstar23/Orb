/**
 * Laboratory values as the bench types them (lis-10). The server decides;
 * these rules let the entry grid say what is wrong first, in the same words.
 *
 *   · More decimal places than the test reports is refused, never rounded.
 *   · Fewer is the same number, printed at the test's precision.
 *   · A comparator keeps its bound, held to the same precision.
 *   · Arithmetic is on digits: no float ever enters a value.
 */
import { describe, expect, it } from 'vitest'

import { flagWords, formatMilli, matchCodedValue, parseLabNumber } from './labValue'

describe('parseLabNumber', () => {
  it.each([
    ['4.5', 1, null, 4500, '4.5'],
    ['4.50', 1, null, 4500, '4.5'],
    ['4', 1, null, 4000, '4.0'],
    ['1.2', 2, null, 1200, '1.20'],
    ['0.1', 3, null, 100, '0.100'],
    ['<0.01', 2, '<', 10, '<0.01'],
    ['> 600', 0, '>', 600000, '>600'],
    ['≤0.5', 1, '<=', 500, '<=0.5'],
    ['-2', 0, null, -2000, '-2'],
    ['.5', 1, null, 500, '0.5'],
  ])('reads %s at %i places', (typed, places, comparator, milli, text) => {
    const parsed = parseLabNumber(typed as string, places as number)
    expect(parsed).toEqual({ ok: true, comparator, milli, text })
  })

  it('refuses more decimal places than the test reports, and says so', () => {
    const parsed = parseLabNumber('4.56', 1, 'Potassium')
    expect(parsed.ok).toBe(false)
    if (!parsed.ok) {
      expect(parsed.code).toBe('precision')
      expect(parsed.message).toBe(
        'Potassium is reported in 1 decimal place. Type the value as the report prints it; Orb does not round a result.')
    }
    expect(parseLabNumber('<0.001', 2)).toMatchObject({ ok: false, code: 'precision' })
    expect(parseLabNumber('5.1', 0)).toMatchObject({ ok: false, code: 'precision' })
  })

  it.each(['', '   ', 'four', '1e3', '1,000', '.', '-', '<', '4.2.1'])(
    'refuses %j', (typed) => {
      expect(parseLabNumber(typed, 1).ok).toBe(false)
    })

  it('never lets a float into the value', () => {
    // 0.1 + 0.2 is 0.30000000000000004 as a float; on digits it is exact.
    expect(parseLabNumber('0.3', 1)).toMatchObject({ ok: true, milli: 300 })
    expect(parseLabNumber('1.005', 3)).toMatchObject({ ok: true, milli: 1005, text: '1.005' })
  })
})

describe('formatMilli', () => {
  it('prints at the reporting precision with trailing zeros', () => {
    expect(formatMilli(4500, 2)).toBe('4.50')
    expect(formatMilli(4500, 0)).toBe('4')
    expect(formatMilli(-2500, 1)).toBe('-2.5')
    expect(formatMilli(0, 1)).toBe('0.0')
    expect(formatMilli(null, 1)).toBe('')
  })
})

describe('matchCodedValue', () => {
  it('matches without regard to case or spacing, in the catalogue spelling', () => {
    const allowed = ['Reactive', 'Non-reactive']
    expect(matchCodedValue('  non-REACTIVE ', allowed)).toBe('Non-reactive')
    expect(matchCodedValue('Weakly reactive', allowed)).toBeNull()
    expect(matchCodedValue('', allowed)).toBeNull()
  })
})

describe('flagWords', () => {
  it('says a flag in words and leaves no flag blank', () => {
    expect(flagWords('H')).toBe('High')
    expect(flagWords('L')).toBe('Low')
    expect(flagWords('')).toBe('')
    expect(flagWords(null)).toBe('')
  })
})
