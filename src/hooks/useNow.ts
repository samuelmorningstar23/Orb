import { useCallback, useSyncExternalStore } from 'react'

/** The current time for RENDERING, re-read every `refreshMs` while `enabled`.
 *
 *  Calling Date.now() straight in a render makes the render impure
 *  (react-hooks/purity): the same props can show a different age. This reads
 *  the clock through useSyncExternalStore instead. The value is rounded UP to
 *  the next whole second, so it stays stable between React's reads and an age
 *  worked out from it can only err older, never fresher, and by under a second.
 *  A staleness label that understates the age is the dangerous direction. */
export function useNow(refreshMs: number, enabled = true): number {
  const subscribe = useCallback((onTick: () => void) => {
    if (!enabled) return () => {}
    const timer = setInterval(onTick, refreshMs)
    return () => clearInterval(timer)
  }, [refreshMs, enabled])
  return useSyncExternalStore(subscribe, readClock)
}

function readClock(): number {
  return Math.ceil(Date.now() / 1000) * 1000
}

/** The time of THIS render, for stamping when something was seen. No ticking:
 *  it re-reads only when the component renders for another reason.
 *
 *  Rounded DOWN to the whole second, the opposite of useNow, for the same
 *  reason: a stamp that is slightly early makes the thing it dates look older,
 *  never fresher. */
export function useClockAtRender(): number {
  return useSyncExternalStore(subscribeNever, readClockFloor)
}

function subscribeNever(): () => void {
  return () => {}
}

function readClockFloor(): number {
  return Math.floor(Date.now() / 1000) * 1000
}
