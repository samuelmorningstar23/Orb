/**
 * BLOOD PRESSURE IS NOT MEASURED TO A TENTH.
 *
 * The handoff sheet and the downtime pack printed whatever float the wire
 * carried — "BP 110.8/62.3 mmHg" on a document that gets taped above a bed.
 * A decimal claims a precision the cuff never had. These pin the display
 * contract; the stored value and everything scored from it are untouched.
 */
import { describe, expect, it } from 'vitest'
import { bloodPressure, observedAt, observedBefore, vitalTemperature, vitalWhole, NO_VALUE } from './vitals'

describe('whole-number vitals', () => {
  it('rounds a float to a whole number', () => {
    expect(vitalWhole(110.8)).toBe('111')
    expect(vitalWhole(62.3)).toBe('62')
    expect(vitalWhole(88)).toBe('88')
  })

  it('renders the dash for a missing reading rather than 0 or NaN', () => {
    expect(vitalWhole(null)).toBe(NO_VALUE)
    expect(vitalWhole(undefined)).toBe(NO_VALUE)
    expect(vitalWhole('')).toBe(NO_VALUE)
    expect(vitalWhole('not a number')).toBe(NO_VALUE)
    // A real zero is a reading, not an absence.
    expect(vitalWhole(0)).toBe('0')
  })

  it('accepts a numeric string off the wire', () => {
    expect(vitalWhole('97.4')).toBe('97')
  })
})

describe('blood pressure', () => {
  it('prints systolic/diastolic with no decimals', () => {
    expect(bloodPressure(110.8, 62.3)).toBe('111/62')
    expect(bloodPressure(110.8, 62.3)).not.toMatch(/\./)
  })

  it('dashes each half independently', () => {
    expect(bloodPressure(120, null)).toBe(`120/${NO_VALUE}`)
    expect(bloodPressure(null, null)).toBe(`${NO_VALUE}/${NO_VALUE}`)
  })

  it('honours a caller-supplied dash', () => {
    expect(bloodPressure(null, null, '--')).toBe('--/--')
  })
})

describe('temperature', () => {
  it('keeps the one decimal a thermometer actually reads', () => {
    expect(vitalTemperature(37)).toBe('37.0')
    expect(vitalTemperature(38.42)).toBe('38.4')
  })

  it('dashes a missing reading', () => {
    expect(vitalTemperature(null)).toBe(NO_VALUE)
  })
})

describe('as charted: the number NEWS2 scored (ruled 2026-10-02)', () => {
  // Each pair is the value NEWS2 scores and its drivers print, from
  // server/services/vital_charting.py. A sheet printing anything else shows
  // a number that contradicts the driver beside it.
  it('rounds a whole-number vital half-up', () => {
    expect(vitalWhole(20.5)).toBe('21')
    expect(vitalWhole(8.5)).toBe('9')
    expect(vitalWhole(20.4)).toBe('20')
    expect(vitalWhole(130.5)).toBe('131')
  })

  it('rounds temperature on its decimal form, not its binary one', () => {
    // toFixed(1) prints every one of these a tenth low.
    expect(vitalTemperature(38.05)).toBe('38.1')
    expect(vitalTemperature(35.05)).toBe('35.1')
    expect(vitalTemperature(36.05)).toBe('36.1')
    expect(vitalTemperature(39.05)).toBe('39.1')
    expect(vitalTemperature(38.04)).toBe('38.0')
    expect(vitalTemperature(36.25)).toBe('36.3')
  })

  it('agrees with the server rule across the temperature range', () => {
    // Literal reimplementation of half-up on the decimal string, so this
    // checks chartedValue against arithmetic it does not share.
    for (let i = 28000; i <= 43000; i += 5) {
      const raw = i / 1000
      const [whole, frac = ''] = String(raw).split('.')
      const padded = (frac + '00').slice(0, 2)
      let tenths = Number(whole) * 10 + Number(padded[0])
      if (Number(padded[1]) >= 5) tenths += 1
      expect(vitalTemperature(raw)).toBe((tenths / 10).toFixed(1))
    }
  })
})

/* Which set is newer. The Vigil chart and the ward board both refuse a tick
   strictly older than the set on screen, through these two. */
describe('observedAt / observedBefore', () => {
  it('reads a bare SQLite stamp as UTC, not local time', () => {
    // Date.parse would read this as local time and move it by the UTC offset.
    expect(observedAt({ timestamp: '2026-10-02 08:00:00' })).toBe(Date.UTC(2026, 9, 2, 8, 0, 0))
    expect(observedAt({ timestamp: '2026-10-02T08:00:00Z' })).toBe(Date.UTC(2026, 9, 2, 8, 0, 0))
    expect(observedAt({ timestamp: '2026-10-02T13:30:00+05:30' })).toBe(Date.UTC(2026, 9, 2, 8, 0, 0))
  })

  it('has no time for a missing, empty, non-string or unparseable stamp', () => {
    for (const v of [null, undefined, {}, { timestamp: '' }, { timestamp: 42 }, { timestamp: 'not a time' }]) {
      expect(observedAt(v)).toBeNull()
    }
  })

  it('is strictly older only: equal is not older, and an undatable set never wins', () => {
    const at8 = { timestamp: '2026-10-02 08:00:00' }
    const at805 = { timestamp: '2026-10-02T08:05:00Z' }
    expect(observedBefore(at8, at805)).toBe(true)
    expect(observedBefore(at805, at8)).toBe(false)
    expect(observedBefore(at8, { timestamp: '2026-10-02T08:00:00Z' })).toBe(false)
    expect(observedBefore({ timestamp: 'garbled' }, at805)).toBe(false)
    expect(observedBefore(at8, null)).toBe(false)
  })
})
