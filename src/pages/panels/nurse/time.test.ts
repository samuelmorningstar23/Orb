/**
 * DEMO_READINESS §5.3 — stale timestamps must carry their date.
 *
 * The nurse board rendered "Last 12:37 AM · obs 144h 33m late": the reading was
 * seven days old but formatted time-only, so it read as an hour ago and the two
 * halves of the same line contradicted each other. The invariant guarded here:
 * clockTime renders a bare clock time ONLY for a same-calendar-day moment;
 * anything older carries its date ("Aug 9, 14:32" shape).
 *
 * Fixtures are written as LOCAL wall-clock times and passed through `local()`,
 * which pins them to an explicit UTC instant, so the assertions hold in any
 * test-runner timezone. A bare string would now be read as UTC (see the
 * SQLite case at the end), which is what the server means by one.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { MedTask } from '../../../api/nurse'
import { clockTime, dueLabel, missedSlotsCount, obsLabel, relativeMinutes, shiftDate } from './time'

// A local wall-clock time as the offset-carrying ISO string the API sends.
const local = (wall: string) => new Date(wall).toISOString()

// The clinical forms, independent of the runner's locale: "02:32 PM" and
// "9 Aug, 02:32 PM".
const pad = (n: number) => String(n).padStart(2, '0')
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const bareTime = (iso: string) => {
  const d = new Date(iso)
  const h = d.getHours() % 12 || 12
  return `${pad(h)}:${pad(d.getMinutes())} ${d.getHours() < 12 ? 'AM' : 'PM'}`
}
const dateTime = (iso: string) => {
  const d = new Date(iso)
  return `${d.getDate()} ${MON[d.getMonth()]}, ${bareTime(iso)}`
}

describe('clockTime (§5.3)', () => {
  const now = new Date('2026-08-10T18:00:00')

  it('renders a same-day moment as a bare clock time', () => {
    const iso = local('2026-08-10T14:32:00')
    expect(clockTime(iso, now)).toBe(bareTime(iso))
  })

  it('renders an older moment with its date, never as a bare time', () => {
    const iso = local('2026-08-09T14:32:00')
    expect(clockTime(iso, now)).toBe(dateTime(iso))
    expect(clockTime(iso, now)).not.toBe(bareTime(iso))
  })

  it('dates a moment from ten minutes ago if midnight has passed since', () => {
    // 23:55 yesterday vs 00:05 today: minutes apart, but a bare "11:55 PM"
    // tomorrow morning would read as tonight — the exact §5.3 failure mode.
    const justAfterMidnight = new Date('2026-08-10T00:05:00')
    const iso = local('2026-08-09T23:55:00')
    expect(clockTime(iso, justAfterMidnight)).toBe(dateTime(iso))
  })

  it('dates the seven-day-stale reading from the §5.3 repro', () => {
    const iso = local('2026-08-03T00:37:00')
    expect(clockTime(iso, now)).toBe(dateTime(iso))
  })

  it('reads a bare SQLite timestamp as UTC, not as local time', () => {
    // CURRENT_TIMESTAMP writes "2026-08-10 13:21:00" with no zone and means
    // UTC. new Date() read it as local, so a ward dispense at 06:51 PM IST
    // printed as 01:21 PM on the deliveries sheet.
    const sqlite = '2026-08-10 13:21:00'
    const instant = new Date(Date.UTC(2026, 7, 10, 13, 21))
    const sameDayNow = new Date(instant.getTime() + 60_000)
    expect(clockTime(sqlite, sameDayNow)).toBe(bareTime(instant.toISOString()))
  })

  it('writes the clinical forms whatever the browser locale', () => {
    expect(clockTime(local('2026-08-10T14:32:00'), now)).toBe('02:32 PM')
    expect(clockTime(local('2026-08-09T14:32:00'), now)).toBe('9 Aug, 02:32 PM')
    expect(clockTime(local('2025-12-31T09:05:00'), now)).toBe('31 Dec 2025, 09:05 AM')
  })

  it('returns an em dash for missing or unparseable input', () => {
    expect(clockTime(null, now)).toBe('—')
    expect(clockTime(undefined, now)).toBe('—')
    expect(clockTime('not-a-timestamp', now)).toBe('—')
  })
})

describe('obsLabel carries the dated form through (§5.3)', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('a days-old last-obs no longer reads as an hour ago', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-10T18:00:00'))
    const staleIso = local('2026-08-03T00:37:00')
    const label = obsLabel(staleIso, -8673)
    // The lateness is capped in words past a day; the dated clock says when.
    expect(label).toBe(`Last ${dateTime(staleIso)} · obs over 24h late`)
    expect(label).not.toContain(`Last ${bareTime(staleIso)} ·`)
  })

  it('a same-day last-obs stays a bare clock time', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-10T18:00:00'))
    const todayIso = local('2026-08-10T12:30:00')
    expect(obsLabel(todayIso, -90)).toBe(`Last ${bareTime(todayIso)} · obs 1h 30m late`)
  })
})

describe('shiftDate', () => {
  it('writes the weekday and date as "Mon 10 Aug"', () => {
    expect(shiftDate(local('2026-08-10T09:00:00'))).toBe('Mon 10 Aug')
  })
})

describe('display caps', () => {
  it('caps lateness past a day in words, and leaves anything shorter exact', () => {
    expect(relativeMinutes(-8673)).toBe('over 24h late')
    expect(relativeMinutes(-1441)).toBe('over 24h late')
    expect(relativeMinutes(-1440)).toBe('24h late')
    expect(relativeMinutes(-42)).toBe('42m late')
    // Only lateness is capped: a due time in the future is never "late".
    expect(relativeMinutes(2000)).toBe('in 33h 20m')
  })

  it('caps the uncharted earlier doses at 12+', () => {
    expect(missedSlotsCount(1)).toBe('1')
    expect(missedSlotsCount(12)).toBe('12')
    expect(missedSlotsCount(13)).toBe('12+')
    expect(missedSlotsCount(168)).toBe('12+')
  })
})

describe('dueLabel for a continuous order', () => {
  it('says there is no due time, not "Continuous" a second time', () => {
    const infusion = { schedule_kind: 'continuous', due_at: null } as unknown as MedTask
    expect(dueLabel(infusion)).toBe('No due time')
  })
})
