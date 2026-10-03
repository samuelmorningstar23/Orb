/**
 * THE SPARKLINE WINDOW IS CHOSEN BY TIME, AND ITS LABEL IS THE SPAN IT COVERS.
 *
 * The command-bar trend took the newest 40 entries and called them six hours.
 * These pin the window (at most 6 hours ending at the newest observation, any
 * input order), the rows it must refuse (no timestamp, no number), the
 * thinning (a spike survives), and the words the header and aria-label use.
 * The DOM half lives in src/pages/panels/VigilPanel.sparkline.test.tsx.
 */
import { describe, expect, it } from 'vitest'
import {
  TREND_MAX_POINTS, TREND_WINDOW_MS, trendAriaPhrase, trendAxisLabel,
  trendSpanShort, trendSpanSpoken, vitalTrendWindow,
} from './vitalTrend'

const T0 = Date.UTC(2026, 9, 3, 6, 0, 0)
const MIN = 60_000
const HOUR = 60 * MIN
const at = (ms: number) => new Date(ms).toISOString()

describe('the window', () => {
  it('keeps at most the six hours that end at the newest observation', () => {
    const rows = [0, 2, 5, 7, 9].map(h => ({ timestamp: at(T0 + h * HOUR), heart_rate: 80 + h }))
    const win = vitalTrendWindow(rows, 'heart_rate')!
    // 9 h is the newest, so 3 h onward is inside: 5, 7, 9.
    expect(win.points.map(p => p.v)).toEqual([85, 87, 89])
    expect(win.spanMs).toBe(4 * HOUR)
    expect(win.firstMs).toBe(T0 + 5 * HOUR)
    expect(win.lastMs).toBe(T0 + 9 * HOUR)
  })

  it('includes an observation exactly six hours before the newest', () => {
    const rows = [0, 6].map(h => ({ timestamp: at(T0 + h * HOUR), heart_rate: 70 + h }))
    expect(vitalTrendWindow(rows, 'heart_rate')!.spanMs).toBe(TREND_WINDOW_MS)
  })

  it('does not trust the input order', () => {
    const newestFirst = [3, 2, 1, 0].map(i => ({ timestamp: at(T0 + i * MIN), spo2: 90 + i }))
    const shuffled = [newestFirst[2], newestFirst[0], newestFirst[3], newestFirst[1]]
    expect(vitalTrendWindow(shuffled, 'spo2')!.points.map(p => p.v)).toEqual([90, 91, 92, 93])
  })

  it('reads bare SQLite UTC timestamps and tz-suffixed ones on one axis', () => {
    const rows = [
      { timestamp: '2026-10-03 06:00:00', heart_rate: 80 },
      { timestamp: '2026-10-03T11:30:00+05:30', heart_rate: 90 }, // 06:00 UTC + 0
      { timestamp: '2026-10-03T06:10:00Z', heart_rate: 100 },
    ]
    const win = vitalTrendWindow(rows, 'heart_rate')!
    expect(win.spanMs).toBe(10 * MIN)
    expect(win.lastMs).toBe(Date.UTC(2026, 9, 3, 6, 10))
  })

  it('drops a row with no timestamp instead of plotting it as "now"', () => {
    // parseUTC('') returns the current time, which would make an undated row
    // the newest observation and drag the whole window to today.
    const rows = [
      { timestamp: at(T0), heart_rate: 80 },
      { timestamp: at(T0 + 5 * MIN), heart_rate: 82 },
      { timestamp: '', heart_rate: 150 },
      { heart_rate: 151 },
      { timestamp: 'not a time', heart_rate: 152 },
    ]
    const win = vitalTrendWindow(rows, 'heart_rate')!
    expect(win.count).toBe(2)
    expect(win.max).toBe(82)
  })

  it('skips a vital that was not recorded on a row, but reads a numeric string', () => {
    const rows = [
      { timestamp: at(T0), temperature: 37.1 },
      { timestamp: at(T0 + 10 * MIN), temperature: null },
      { timestamp: at(T0 + 15 * MIN), temperature: '' },
      { timestamp: at(T0 + 20 * MIN), temperature: '38.0' },
      { timestamp: at(T0 + 25 * MIN), temperature: Number.NaN },
      { timestamp: at(T0 + 30 * MIN), temperature: 37.6 },
    ]
    expect(vitalTrendWindow(rows, 'temperature')!.points.map(p => p.v)).toEqual([37.1, 38, 37.6])
  })

  it('has no trend for fewer than two readings or a single instant', () => {
    expect(vitalTrendWindow([], 'heart_rate')).toBeNull()
    expect(vitalTrendWindow(undefined, 'heart_rate')).toBeNull()
    expect(vitalTrendWindow([{ timestamp: at(T0), heart_rate: 80 }], 'heart_rate')).toBeNull()
    expect(vitalTrendWindow([
      { timestamp: at(T0), heart_rate: 80 },
      { timestamp: at(T0), heart_rate: 84 },
    ], 'heart_rate')).toBeNull()
  })

  it('has no trend when the only earlier reading is outside the window', () => {
    const rows = [
      { timestamp: at(T0), heart_rate: 80 },
      { timestamp: at(T0 + 8 * HOUR), heart_rate: 84 },
    ]
    expect(vitalTrendWindow(rows, 'heart_rate')).toBeNull()
  })
})

describe('thinning a dense window', () => {
  // Six hours at the 5-second lane's cadence, capped where the live array caps.
  const rows = Array.from({ length: 2000 }, (_, i) => ({
    timestamp: at(T0 + i * 5_000),
    heart_rate: i === 1234 ? 171 : i === 777 ? 41 : 80 + (i % 5),
  }))
  const win = vitalTrendWindow(rows, 'heart_rate')!

  it('draws no more points than the line can show', () => {
    expect(win.count).toBe(2000)
    expect(win.points.length).toBeLessThanOrEqual(TREND_MAX_POINTS)
  })

  it('keeps a one-reading spike and dip that a stride would drop', () => {
    const drawn = win.points.map(p => p.v)
    expect(drawn).toContain(171)
    expect(drawn).toContain(41)
    expect(win.max).toBe(171)
    expect(win.min).toBe(41)
  })

  it('still runs from the first observation to the last, in time order', () => {
    expect(win.points[0].t).toBe(win.firstMs)
    expect(win.points[win.points.length - 1].t).toBe(win.lastMs)
    const times = win.points.map(p => p.t)
    expect(times).toEqual([...times].sort((a, b) => a - b))
  })
})

describe('the words', () => {
  it('states the span in the header form', () => {
    expect(trendSpanShort(45_000)).toBe('45 S')
    expect(trendSpanShort(59_600)).toBe('1 MIN')
    expect(trendSpanShort(295_000)).toBe('5 MIN')
    expect(trendSpanShort(4 * HOUR)).toBe('4H')
    expect(trendSpanShort(3 * HOUR + 10 * MIN)).toBe('3H 10M')
    expect(trendSpanShort(TREND_WINDOW_MS)).toBe('6H')
  })

  it('states the span in words for a screen reader', () => {
    expect(trendSpanSpoken(1_000)).toBe('1 second')
    expect(trendSpanSpoken(45_000)).toBe('45 seconds')
    expect(trendSpanSpoken(295_000)).toBe('5 min')
    expect(trendSpanSpoken(HOUR)).toBe('1 hour')
    expect(trendSpanSpoken(3 * HOUR + 10 * MIN)).toBe('3 hours 10 min')
  })

  it('prints the date on an axis end only when it is not today', () => {
    const noon = new Date(2026, 9, 3, 12, 0).getTime()
    const morning = new Date(2026, 9, 3, 8, 5).getTime()
    const twoDaysAgo = new Date(2026, 9, 1, 22, 40).getTime()
    expect(trendAxisLabel(morning, noon)).toEqual({ date: null, time: '08:05 AM' })
    expect(trendAxisLabel(twoDaysAgo, noon)).toEqual({ date: '1 Oct', time: '10:40 PM' })
  })

  it('names the span and both ends in the aria phrase', () => {
    const noon = new Date(2026, 9, 3, 12, 0).getTime()
    const rows = [
      { timestamp: new Date(2026, 9, 2, 23, 30).toISOString(), heart_rate: 90 },
      { timestamp: new Date(2026, 9, 3, 2, 0).toISOString(), heart_rate: 96 },
    ]
    const win = vitalTrendWindow(rows, 'heart_rate')!
    expect(trendAriaPhrase(win, 'systolic trend', noon))
      .toBe('open systolic trend, 2 hours 30 min of observations, 2 Oct 11:30 PM to 02:00 AM')
  })
})
