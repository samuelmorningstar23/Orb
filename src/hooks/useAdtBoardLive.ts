import { useEffect, useRef } from 'react'

import { subscribeAdtChanged, subscribeSocketReconnect } from '../api/adt/live'
import { DATA_CHANGED_EVENT } from '../contexts/PanelActiveContext'

/** How long a burst of 'adt:changed' events waits before its one refetch. A
 *  move between two wards is two events (one per ward); they are read once. */
export const ADT_LIVE_COALESCE_MS = 250

/**
 * Re-read a bed board when the server says beds and admissions changed
 * (adt-19). The event names only a ward (src/api/adt/live.ts), so the board
 * re-reads over its own scoped, audited REST call; nothing about a patient
 * arrives on the socket.
 *
 *   * `wardId` is the ward the board shows, or '' / null for all the
 *     caller's wards. An event naming another ward is ignored; an event
 *     naming no ward (a patient waiting for a bed) always refetches.
 *   * A burst of events inside ADT_LIVE_COALESCE_MS is one refetch.
 *   * When the socket comes back after dropping, the board re-reads once,
 *     for whatever it missed while it was down.
 *   * A change made on this screen (the shell's 'orb:data-changed', for
 *     example after the ward clerk admits a patient) re-reads it too, with no
 *     wait for the socket.
 *
 * The board keeps its own 30 second poll whatever the socket does: it is the
 * fallback while the socket is down, and it also catches what no write
 * announces (a reservation running out, the minutes a bed has been waiting).
 */
export function useAdtBoardLive(refetch: () => void, wardId: string | null | undefined): void {
  const saved = useRef(refetch)
  useEffect(() => { saved.current = refetch }, [refetch])

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null
    const schedule = () => {
      if (timer !== null) return
      timer = setTimeout(() => {
        timer = null
        saved.current()
      }, ADT_LIVE_COALESCE_MS)
    }
    const offChanged = subscribeAdtChanged((event) => {
      if (wardId && event.ward_id && event.ward_id !== wardId) return
      schedule()
    })
    const offReconnect = subscribeSocketReconnect(schedule)
    window.addEventListener(DATA_CHANGED_EVENT, schedule)
    return () => {
      offChanged()
      offReconnect()
      window.removeEventListener(DATA_CHANGED_EVENT, schedule)
      if (timer !== null) clearTimeout(timer)
    }
  }, [wardId])
}
