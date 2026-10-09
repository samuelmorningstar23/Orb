/**
 * Queue waits that keep running between reads.
 *
 * A queue read carries the server's wait figures as of that read. A screen
 * that re-reads only when the server nudges it (opd-9b) would otherwise print
 * those figures unchanged for as long as the queue stays quiet: a patient who
 * has waited 50 minutes would still read 5 minutes, in the wrong colour band.
 * These helpers add the whole minutes since the read to every wait that is
 * still running.
 *
 * The minutes are measured between two instants on this screen's monotonic
 * clock (performance.now), so a workstation clock that is wrong by hours, or
 * is reset while a patient waits, never moves a figure. The server floors its
 * minutes and so does this, so a figure is the one a fresh read would print or
 * at most one minute under it, plus up to one tick.
 */
import { useEffect, useState } from 'react'

/** How often the count is re-checked. It changes only once a minute, and a
 *  re-render happens only when it does. */
export const WAIT_TICK_MS = 15_000

/**
 * Whole minutes since `snapshot` arrived. Snapshots are compared by identity:
 * each new read starts again from 0, because its figures are already current.
 */
export function useMinutesSinceRead(snapshot: unknown, tickMs: number = WAIT_TICK_MS): number {
  const [count, setCount] = useState<{ snapshot: unknown; minutes: number }>(
    () => ({ snapshot, minutes: 0 }))
  useEffect(() => {
    const readAt = performance.now()
    const timer = setInterval(() => {
      const minutes = Math.max(0, Math.floor((performance.now() - readAt) / 60_000))
      setCount(prev => (prev.snapshot === snapshot && prev.minutes === minutes
        ? prev
        : { snapshot, minutes }))
    }, tickMs)
    return () => clearInterval(timer)
  }, [snapshot, tickMs])
  return count.snapshot === snapshot ? count.minutes : 0
}

/** The fields of a queue visit that decide whether its wait is still running. */
export interface WaitingVisit {
  status: string
  called_at?: string | null
  waiting_minutes: number | null
}

/**
 * A visit's wait as of now. Only a wait the server measured up to the moment
 * of its read grows: a waiting visit not yet called. The server stops the
 * clock at the call, the cancel or the finish, so those keep its figure. An
 * unmeasured wait stays unknown, never a number.
 */
export function liveWaitMinutes(visit: WaitingVisit, minutesSinceRead: number): number | null {
  const base = visit.waiting_minutes
  if (base === null || base === undefined || !Number.isFinite(base)) return null
  if (visit.status !== 'waiting' || visit.called_at) return base
  return base + Math.max(0, minutesSinceRead)
}

/**
 * A queue's longest wait as of now: the longest running wait among its
 * waiting visits, worked out the same way the server does, so it always
 * matches the longest row on screen. Null, as the server sends it, when
 * nothing waiting has a measured wait: no queue is not the same as no wait.
 */
export function liveLongestWait(
  visits: readonly WaitingVisit[],
  serverLongest: number | null,
  minutesSinceRead: number,
): number | null {
  if (serverLongest === null || serverLongest === undefined || !Number.isFinite(serverLongest)) {
    return null
  }
  const waits = visits
    .filter(v => v.status === 'waiting')
    .map(v => liveWaitMinutes(v, minutesSinceRead))
    .filter((m): m is number => m !== null)
  return waits.length ? Math.max(...waits) : serverLongest + Math.max(0, minutesSinceRead)
}
