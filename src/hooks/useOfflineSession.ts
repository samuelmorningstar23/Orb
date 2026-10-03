import { useEffect, useRef } from 'react'
import { cacheEntryCount, clearOfflineCache, newestCachedAt } from '../lib/offline/phiCache'
import { clearForeignOfflineQueue, clearOfflineQueue } from '../api/client'

/**
 * Ties the PHI cache's lifetime to ONE clinician's session.
 *
 * The offline cache is the only part of Orb that deliberately leaves a nurse's
 * patients sitting on a device. On a personal workstation that would be a minor
 * concern; on the trolley tablet this is built for — six nurses a shift, a bay
 * of named patients on each of them — it is the whole risk. So the cache has to
 * die at every point the device could change hands, and there are three of
 * them, not one:
 *
 *   SIGN-OUT       the clean path. App.logout() also clears synchronously; this
 *                  effect is the backstop for any future sign-out route that
 *                  forgets to, because it watches the user ID rather than the
 *                  call site.
 *   USER SWITCH    nurse B signs in while nurse A's session is still the one in
 *                  memory (a PIN pad handover on a shared tablet). No logout
 *                  ran, so nothing else would have fired.
 *   CRASH RECOVERY the tablet's battery died mid-round, or the browser was
 *                  force-closed. No logout ran and no switch was observed — the
 *                  app simply boots with a cache from a session that has no
 *                  code path left to end it. Handled on mount by asking whether
 *                  the entries on the device belong to the person now holding
 *                  it, using phiCache's own ownership rules rather than a
 *                  second copy of them here.
 *
 * phiCache also refuses another user's entry on READ (and wipes the device when
 * it sees one), which no crash can skip. This hook is the proactive half: it
 * does not wait for a read to happen before removing PHI that is already
 * nobody's business.
 *
 * TWO STORES, ONE LIFETIME. The read cache is only half the PHI on the device:
 * api/client also holds an offline WRITE queue (free clinical text, patient ids,
 * dose payloads) which was surviving every handover and then replaying on
 * whoever was signed in next — the previous nurse's note filed under this
 * nurse's name. Both stores are bound to the session here, together, so there is
 * one place to check rather than two places to forget.
 */
export function useOfflineSession(userId: string | null | undefined): void {
  const previous = useRef<string | null | undefined>(undefined)

  useEffect(() => {
    const current = userId ?? null
    const prior = previous.current
    previous.current = current

    if (prior === undefined) {
      // FIRST RUN — a mount, which is also every page refresh and every cold
      // boot after a crash. There is no previous ID in memory to compare
      // against, so ask the cache instead: are any entries on this device
      // readable by the person signed in right now?
      //
      // newestCachedAt applies the real ownership and age rules. Entries
      // present but none of them this user's fresh data means the device is
      // carrying somebody else's PHI (or expired PHI). Either way it goes.
      if (cacheEntryCount() > 0 && (!current || newestCachedAt(current) === null)) {
        clearOfflineCache()
      }
      // The queue carries its own author on every entry, so it can be judged
      // exactly rather than all-or-nothing: this user's pending writes survive a
      // page refresh mid-shift, anybody else's do not survive the boot. Silent
      // by design — the person who would be told is not the person who queued
      // them, and telling THIS nurse that work she never did was discarded is
      // just a confusing alarm.
      const dropped = clearForeignOfflineQueue(current)
      if (dropped.length > 0) {
        console.warn(`Discarded ${dropped.length} offline write(s) queued by a previous session.`)
      }
      return
    }

    // Sign-out (current === null) or a switch to a different clinician. Both
    // stores go: the cache is what the last nurse could READ, the queue is what
    // she tried to WRITE, and neither belongs to whoever picks the tablet up.
    if (prior !== current) {
      clearOfflineCache()
      clearOfflineQueue()
    }
  }, [userId])
}
