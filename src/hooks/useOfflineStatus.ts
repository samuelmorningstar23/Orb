import { useEffect, useRef, useState } from 'react'

/**
 * Is this device connected, and since when?
 *
 * `navigator.onLine` is a weak signal and the whole offline feature rests on
 * it, so it is worth being precise about what it does and does not mean. It
 * reports whether the browser has a usable NETWORK INTERFACE — not whether
 * Orb's appliance in the server room is answering. A tablet on ward wi-fi with
 * the appliance rebooting reads as ONLINE here.
 *
 * That asymmetry decides how the two signals are used:
 *
 *   `online === false`  is TRUSTED. There is no network at all, so nothing can
 *                       be written and nothing fresh can be read. This is the
 *                       corridor-with-no-signal case, and it is unambiguous.
 *   `online === true`   is NOT trusted as proof of anything. A fetch can still
 *                       fail. Surfaces therefore also pass their own load
 *                       failure into useOfflineSnapshot (`liveFailed`), which
 *                       is what covers "wi-fi is fine, the server is not".
 *
 * Read the events, not a poll: the browser fires online/offline on the actual
 * interface transition, and polling navigator.onLine would just re-read the
 * same value on a timer.
 */
export interface OfflineStatus {
  online: boolean
  /** Epoch ms of the moment the connection dropped; null while online. */
  offlineSince: number | null
}

function readOnline(): boolean {
  if (typeof navigator === 'undefined') return true
  return navigator.onLine !== false
}

export function useOfflineStatus(): OfflineStatus {
  const [online, setOnline] = useState(readOnline)
  const [offlineSince, setOfflineSince] = useState<number | null>(() =>
    readOnline() ? null : Date.now(),
  )

  useEffect(() => {
    const goOnline = () => { setOnline(true); setOfflineSince(null) }
    const goOffline = () => {
      setOnline(false)
      // Only stamp the FIRST drop — the browser can fire 'offline' repeatedly
      // while an interface flaps, and restamping would keep resetting the
      // "offline for N minutes" reading back to zero.
      setOfflineSince(prev => prev ?? Date.now())
    }
    window.addEventListener('online', goOnline)
    window.addEventListener('offline', goOffline)
    // The interface can change between the initial render and this effect
    // attaching (a slow first paint on a tablet waking from sleep).
    if (readOnline() !== online) (readOnline() ? goOnline : goOffline)()
    return () => {
      window.removeEventListener('online', goOnline)
      window.removeEventListener('offline', goOffline)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return { online, offlineSince }
}

/**
 * Run a callback the moment the connection comes back.
 *
 * Separate from the status hook because the thing a panel wants on reconnect is
 * a side effect (refetch), and it must not depend on the identity of the
 * callback the caller happens to pass this render — a fresh closure per render
 * would tear down and re-attach the listener constantly, and could miss the
 * event in between.
 */
export function useOnReconnect(callback: (() => void) | undefined): void {
  const ref = useRef(callback)
  ref.current = callback

  useEffect(() => {
    const fire = () => ref.current?.()
    window.addEventListener('online', fire)
    return () => window.removeEventListener('online', fire)
  }, [])
}
