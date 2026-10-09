/**
 * Queue waits keep running between reads (the opd-9b desk re-reads only on a
 * nudge, so a quiet queue would otherwise freeze its waits).
 */
import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { liveLongestWait, liveWaitMinutes, useMinutesSinceRead } from './useLiveWaits'

const waiting = (minutes: number | null, extra: Record<string, unknown> = {}) =>
  ({ status: 'waiting', called_at: null, waiting_minutes: minutes, ...extra })

describe('liveWaitMinutes', () => {
  it('adds the minutes since the read to a wait still running', () => {
    expect(liveWaitMinutes(waiting(5), 0)).toBe(5)
    expect(liveWaitMinutes(waiting(5), 45)).toBe(50)
  })

  it('keeps the server figure where the server stopped the clock', () => {
    expect(liveWaitMinutes({ status: 'in_consultation', called_at: '2026-10-05T03:10:00Z',
      waiting_minutes: 10 }, 45)).toBe(10)
    expect(liveWaitMinutes({ status: 'completed', waiting_minutes: 10 }, 45)).toBe(10)
    expect(liveWaitMinutes({ status: 'cancelled', waiting_minutes: 10 }, 45)).toBe(10)
    // The server ends the wait at called_at whatever the status says.
    expect(liveWaitMinutes(waiting(10, { called_at: '2026-10-05T03:10:00Z' }), 45)).toBe(10)
  })

  it('leaves an unmeasured wait unknown, never a number', () => {
    expect(liveWaitMinutes(waiting(null), 45)).toBeNull()
  })

  it('never runs a wait backwards', () => {
    expect(liveWaitMinutes(waiting(5), -3)).toBe(5)
  })
})

describe('liveLongestWait', () => {
  it('is the longest running wait, matching the longest row', () => {
    const visits = [waiting(5), waiting(12),
      { status: 'in_consultation', called_at: '2026-10-05T03:10:00Z', waiting_minutes: 40 }]
    expect(liveLongestWait(visits, 12, 0)).toBe(12)
    expect(liveLongestWait(visits, 12, 30)).toBe(42)
  })

  it('stays null when the server says nothing waiting has a wait', () => {
    expect(liveLongestWait([], null, 30)).toBeNull()
    expect(liveLongestWait([waiting(null)], null, 30)).toBeNull()
  })
})

function Probe({ snapshot, onMinutes }: { snapshot: unknown; onMinutes: (m: number) => void }) {
  onMinutes(useMinutesSinceRead(snapshot))
  return null
}

describe('useMinutesSinceRead', () => {
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('counts whole minutes since the read and restarts on a new read', () => {
    const seen: number[] = []
    const first = { read: 1 }
    const view = render(<Probe snapshot={first} onMinutes={m => seen.push(m)} />)
    expect(seen.at(-1)).toBe(0)
    act(() => { vi.advanceTimersByTime(59_000) })
    expect(seen.at(-1)).toBe(0)
    act(() => { vi.advanceTimersByTime(16_000) })
    expect(seen.at(-1)).toBe(1)
    act(() => { vi.advanceTimersByTime(49 * 60_000) })
    expect(seen.at(-1)).toBe(50)

    // The same read re-rendered keeps its count.
    view.rerender(<Probe snapshot={first} onMinutes={m => seen.push(m)} />)
    expect(seen.at(-1)).toBe(50)

    // A fresh read is already current: back to 0, then counting again.
    view.rerender(<Probe snapshot={{ read: 2 }} onMinutes={m => seen.push(m)} />)
    expect(seen.at(-1)).toBe(0)
    act(() => { vi.advanceTimersByTime(2 * 60_000 + 15_000) })
    expect(seen.at(-1)).toBe(2)
  })

  it('re-renders only when the minute changes', () => {
    const seen: number[] = []
    render(<Probe snapshot={{ read: 1 }} onMinutes={m => seen.push(m)} />)
    const renders = seen.length
    act(() => { vi.advanceTimersByTime(45_000) })
    expect(seen.length).toBe(renders)
  })

  it('ignores the wall clock being reset while a patient waits', () => {
    const seen: number[] = []
    render(<Probe snapshot={{ read: 1 }} onMinutes={m => seen.push(m)} />)
    act(() => { vi.setSystemTime(Date.now() + 3 * 3_600_000) })
    act(() => { vi.advanceTimersByTime(60_000) })
    expect(seen.at(-1)).toBe(1)
  })
})
