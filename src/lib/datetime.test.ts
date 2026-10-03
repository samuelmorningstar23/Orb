import { describe, expect, it } from 'vitest'
import {
  formatClinicalClock, formatClinicalDate, formatClinicalDateKey, formatClinicalDateTime, formatClinicalDayShort, formatClinicalTime, formatDayDivider,
  formatUTCDate, formatUTCDateTime, formatUTCTime, matchesDateSearch, parseDateSearch,
} from './datetime'

// A date whose day and month are both <= 12 is the dangerous case: 1 October
// written numerically is "10/1" in en-US and "1/10" in India. The clinical
// formatters must never produce a numeric month, whatever the browser locale.
const OCT_1 = new Date(2026, 9, 1, 18, 36, 58)

describe('clinical date formatting', () => {
  it('writes the month as a word, day first', () => {
    expect(formatClinicalDate(OCT_1)).toBe('1 Oct 2026')
  })

  it('adds a 12-hour time, with seconds only when asked', () => {
    expect(formatClinicalDateTime(OCT_1)).toBe('1 Oct 2026, 06:36 PM')
    expect(formatClinicalDateTime(OCT_1, { seconds: true })).toBe('1 Oct 2026, 06:36:58 PM')
  })

  it('never renders a numeric day/month pair', () => {
    for (const s of [formatClinicalDate(OCT_1), formatClinicalDateTime(OCT_1), formatUTCDateTime(OCT_1.toISOString())]) {
      expect(s).not.toMatch(/\b\d{1,2}\/\d{1,2}\/\d{2,4}\b/)
    }
  })

  it('reads a bare SQLite timestamp as UTC, not local time', () => {
    const bare = '2026-10-01 13:06:58'
    expect(formatClinicalDateTime(bare, { seconds: true }))
      .toBe(formatClinicalDateTime(new Date(Date.UTC(2026, 9, 1, 13, 6, 58)), { seconds: true }))
  })

  it('returns an empty string for missing or unparseable values', () => {
    expect(formatClinicalDate(null)).toBe('')
    expect(formatClinicalDateTime(undefined)).toBe('')
    expect(formatClinicalDateTime('not a date')).toBe('')
    expect(formatUTCDateTime(null)).toBe('')
  })

  it('formatUTCDate defaults to the clinical form but honours an explicit locale', () => {
    const iso = OCT_1.toISOString()
    expect(formatUTCDate(iso)).toBe('1 Oct 2026')
    expect(formatUTCDate(iso, 'en-GB', { day: '2-digit', month: '2-digit' })).toBe('01/10')
  })
})

describe('clinical time and short day', () => {
  it('writes the time in the 12-hour clinical form with a plain space', () => {
    expect(formatClinicalTime(OCT_1)).toBe('06:36 PM')
    expect(formatClinicalTime(OCT_1, { seconds: true })).toBe('06:36:58 PM')
    expect(formatClinicalTime(OCT_1)).not.toMatch(/[\u202F\u00A0]/)
    expect(formatClinicalTime(null)).toBe('')
  })

  it('formatUTCTime defaults to the clinical time rather than the browser locale', () => {
    expect(formatUTCTime(OCT_1.toISOString())).toBe('06:36 PM')
  })

  it('writes a short day as "Thu 1 Oct"', () => {
    expect(formatClinicalDayShort(OCT_1)).toBe('Thu 1 Oct')
  })

  it('writes a server HH:MM slot as the clinical 12-hour time', () => {
    expect(formatClinicalClock('08:00')).toBe('08:00 AM')
    expect(formatClinicalClock('8:05')).toBe('08:05 AM')
    expect(formatClinicalClock('00:30')).toBe('12:30 AM')
    expect(formatClinicalClock('12:00')).toBe('12:00 PM')
    expect(formatClinicalClock('18:45:00')).toBe('06:45 PM')
    expect(formatClinicalClock('soon')).toBe('soon')
    expect(formatClinicalClock(null)).toBe('')
  })

  it('writes a YYYY-MM-DD key as a clinical date in the viewer calendar', () => {
    expect(formatClinicalDateKey('2026-10-01')).toBe('1 Oct 2026')
    expect(formatClinicalDateKey('')).toBe('')
    expect(formatClinicalDateKey('next week')).toBe('next week')
  })

  it('abbreviates every month to three letters, September included', () => {
    expect(formatClinicalDate(new Date(2026, 8, 30))).toBe('30 Sep 2026')
  })
})

describe('day dividers', () => {
  const now = new Date(2026, 9, 1, 9, 0)
  it('says Today and Yesterday, then the date', () => {
    expect(formatDayDivider(new Date(2026, 9, 1, 0, 5), now)).toBe('Today')
    expect(formatDayDivider(new Date(2026, 8, 30, 23, 59), now)).toBe('Yesterday')
    expect(formatDayDivider(new Date(2026, 8, 29, 12, 0), now)).toBe('29 Sep')
    expect(formatDayDivider(new Date(2025, 11, 31, 12, 0), now)).toBe('31 Dec 2025')
  })
})

describe('date search', () => {
  it('reads the forms a clinician types, day first', () => {
    const oct1 = { year: 2026, month: 10, day: 1 }
    for (const q of ['1 Oct 2026', '1 october 2026', '01/10/2026', '1-10-2026', '01102026', '2026-10-01', 'Oct 1, 2026']) {
      expect(parseDateSearch(q)).toEqual(oct1)
    }
    expect(parseDateSearch('1 Oct')).toEqual({ year: null, month: 10, day: 1 })
  })

  it('leaves words and impossible dates to text search', () => {
    for (const q of ['sepsis', '10 mg', '31 Sep 2026', '00/10/2026', '13/13/2026', '']) {
      expect(parseDateSearch(q)).toBeNull()
    }
  })

  it('matches a timestamp on the searched day in the viewer calendar', () => {
    const q = parseDateSearch('1 Oct 2026')!
    expect(matchesDateSearch(OCT_1, q)).toBe(true)
    expect(matchesDateSearch(new Date(2026, 9, 2, 1, 0), q)).toBe(false)
    expect(matchesDateSearch(new Date(2025, 9, 1, 12, 0), parseDateSearch('1 Oct')!)).toBe(true)
  })
})
