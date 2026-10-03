import { useEffect, useRef } from 'react'
import { DATA_CHANGED_EVENT, usePanelActive } from '../contexts/PanelActiveContext'

/**
 * setInterval that automatically PAUSES while the browser tab is hidden.
 *
 * The clinician shell keeps every visited panel mounted (display:none) so their
 * pollers otherwise keep firing forever — including while the whole tab is
 * backgrounded, which wastes battery/CPU on ward tablets and hammers the
 * backend for data nobody is looking at. This hook gates the interval on
 * document.visibilityState: it stops firing when the tab is hidden and, on
 * becoming visible again, fires once immediately (to refresh stale data) then
 * resumes the interval.
 *
 * It also pauses while the shell panel it lives in is not the one on screen
 * (PanelActiveContext), and fires once when that panel is shown again, so a
 * clinician coming back to a tab sees current data rather than whatever the
 * last poll before they left returned. While running it also refreshes on
 * DATA_CHANGED_EVENT (e.g. a patient was just admitted).
 *
 * Pass enabled=false to pause for the caller's own reasons. The callback is
 * always called via a ref, so changing it between renders never resets the
 * timer.
 */
export function useVisiblePolling(
  callback: () => void,
  intervalMs: number,
  enabled: boolean = true,
): void {
  const savedCallback = useRef(callback)
  // Keep the ref current without touching it during render (react-hooks/refs).
  useEffect(() => { savedCallback.current = callback }, [callback])
  const panelActive = usePanelActive()
  // Set while the panel is hidden: whatever is on screen when it comes back
  // was fetched before the clinician left, so the first run fetches at once.
  const missedWhileHidden = useRef(false)

  useEffect(() => {
    if (!panelActive) {
      missedWhileHidden.current = true
      return
    }
    if (!enabled || intervalMs <= 0) return

    let timer: ReturnType<typeof setInterval> | undefined

    const start = () => {
      if (timer !== undefined) return
      timer = setInterval(() => savedCallback.current(), intervalMs)
    }
    const stop = () => {
      if (timer !== undefined) {
        clearInterval(timer)
        timer = undefined
      }
    }

    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        // Refresh immediately on return, then resume polling.
        missedWhileHidden.current = false
        savedCallback.current()
        start()
      } else {
        stop()
      }
    }
    const onDataChanged = () => {
      if (document.visibilityState === 'visible') savedCallback.current()
    }

    document.addEventListener('visibilitychange', onVisibility)
    window.addEventListener(DATA_CHANGED_EVENT, onDataChanged)
    // Start only if currently visible.
    if (document.visibilityState === 'visible') {
      if (missedWhileHidden.current) {
        missedWhileHidden.current = false
        savedCallback.current()
      }
      start()
    }

    return () => {
      document.removeEventListener('visibilitychange', onVisibility)
      window.removeEventListener(DATA_CHANGED_EVENT, onDataChanged)
      stop()
    }
  }, [intervalMs, enabled, panelActive])
}
