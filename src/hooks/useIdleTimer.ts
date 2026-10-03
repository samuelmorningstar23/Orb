import { useEffect, useRef } from 'react'

/**
 * Fires `onIdle` once after `timeoutMs` of user inactivity.
 *
 * Activity is any of mousemove / keydown / pointerdown / scroll, plus the tab
 * becoming visible again (visibilitychange → visible). Each activity resets the
 * countdown, but resets are throttled to at most ~1/second so a stream of
 * mousemove/scroll events doesn't thrash timers on every frame.
 *
 * `onIdle` is invoked at most once per active period; further activity after
 * idle does NOT re-arm the timer (the caller is expected to unmount / disable
 * the hook by passing `enabled: false` while its lock UI is up, then re-enable
 * once the user has re-authenticated).
 *
 * Pass `enabled: false` to make the hook fully inert — no listeners, no timer —
 * which is how callers suspend idle-locking while a higher-priority blocking
 * modal is already on screen.
 */
export function useIdleTimer(
  timeoutMs: number,
  onIdle: () => void,
  enabled: boolean = true,
): void {
  // Keep the latest callback without re-subscribing listeners on every render.
  const onIdleRef = useRef(onIdle)
  useEffect(() => {
    onIdleRef.current = onIdle
  }, [onIdle])

  useEffect(() => {
    if (!enabled) return

    let timerId: ReturnType<typeof setTimeout> | undefined
    let hasFired = false
    // Timestamp of the last accepted reset, for the ~1/s throttle.
    let lastReset = 0

    const fire = () => {
      if (hasFired) return
      hasFired = true
      onIdleRef.current()
    }

    const arm = () => {
      if (timerId !== undefined) clearTimeout(timerId)
      timerId = setTimeout(fire, timeoutMs)
    }

    const resetThrottled = () => {
      if (hasFired) return
      const now = Date.now()
      if (now - lastReset < 1000) return
      lastReset = now
      arm()
    }

    const handleActivity = () => resetThrottled()

    const handleVisibility = () => {
      if (document.visibilityState === 'visible') resetThrottled()
    }

    // Passive listeners: we never call preventDefault and this keeps scroll
    // handling off the critical path.
    const opts: AddEventListenerOptions = { passive: true }
    window.addEventListener('mousemove', handleActivity, opts)
    window.addEventListener('keydown', handleActivity, opts)
    window.addEventListener('pointerdown', handleActivity, opts)
    window.addEventListener('scroll', handleActivity, opts)
    document.addEventListener('visibilitychange', handleVisibility)

    // Start the countdown immediately on mount / re-enable.
    arm()

    return () => {
      if (timerId !== undefined) clearTimeout(timerId)
      window.removeEventListener('mousemove', handleActivity, opts)
      window.removeEventListener('keydown', handleActivity, opts)
      window.removeEventListener('pointerdown', handleActivity, opts)
      window.removeEventListener('scroll', handleActivity, opts)
      document.removeEventListener('visibilitychange', handleVisibility)
    }
  }, [timeoutMs, enabled])
}

export default useIdleTimer
