import { useEffect, useRef } from 'react'
import { readCache, writeCache } from '../lib/offline/phiCache'
import { getUser } from '../api/client'
import { useOfflineStatus, useOnReconnect } from './useOfflineStatus'

/**
 * The one consumer of lib/offline/phiCache.
 *
 * A panel already owns its own fetch and its own `data` state. This hook wraps
 * that state and answers a single question the panel cannot answer for itself:
 *
 *     is what I am about to render LIVE, or is it a copy?
 *
 * That distinction is the entire feature. A cached shift board is genuinely
 * useful — a nurse in a corridor dead zone still knows who is on her bay and
 * what is overdue. A cached shift board that LOOKS live is dangerous: she reads
 * a NEWS2 from twenty minutes ago as current and walks past a deterioration.
 *
 * Three things follow from that, and they are the whole design:
 *
 *   1. GOING OFFLINE DOWNGRADES DATA THE PANEL ALREADY HAS. React state
 *      survives a wi-fi drop, so without this the panel keeps rendering the
 *      board it fetched before the drop, indefinitely, with no label. The most
 *      likely stale-as-live bug is not a cache read — it is the absence of one.
 *      So when the connection is down, in-memory data is reported as `cache`
 *      with the timestamp of the last LIVE response.
 *   2. THE CACHE IS WRITTEN ONLY FROM A LIVE RESPONSE. Never from a value this
 *      hook is itself replaying, and never from null/undefined — otherwise an
 *      offline mount would overwrite a good copy with nothing, or keep
 *      refreshing `savedAt` so the copy never appeared to age.
 *   3. `online` IS NOT PROOF. A tablet on ward wi-fi with the appliance down
 *      reads as online (see useOfflineStatus). Panels pass their own load
 *      failure as `liveFailed`, which routes to the same labelled fallback with
 *      different words: "can't reach Orb", not "you are offline".
 */

export type SnapshotSource =
  /** Straight from the server, this session. Safe to render unlabelled. */
  | 'live'
  /** A copy. MUST be rendered with OfflineBanner (or equivalent) beside it. */
  | 'cache'
  /** Nothing to show. Say so — an empty ward reads as "no patients". */
  | 'none'

export interface OfflineSnapshot<T> {
  data: T | null
  source: SnapshotSource
  /** When the server actually answered with this data. Null = undatable. */
  savedAt: number | null
  online: boolean
  /** True when the network is up but the fetch failed — the appliance is down,
   *  the tablet is not. Different sentence for the clinician. */
  unreachable: boolean
}

export interface OfflineSnapshotOptions {
  /** Owner of the cache entry. Defaults to the signed-in user from api/client.
   *  Passed explicitly by tests and by anything rendering outside the shell. */
  userId?: string | null
  /** The panel's own load failure. Turns a network-up/server-down fetch error
   *  into a labelled cached view instead of a blank screen. */
  liveFailed?: boolean
  /** Called when the connection returns, so the panel can refetch immediately
   *  rather than waiting out its poll interval. */
  onReconnect?: () => void
}

export function useOfflineSnapshot<T>(
  key: string,
  live: T | null | undefined,
  options: OfflineSnapshotOptions = {},
): OfflineSnapshot<T> {
  const { liveFailed = false, onReconnect } = options
  const userId = options.userId !== undefined ? options.userId : getUser()?.id ?? null

  const { online } = useOfflineStatus()
  useOnReconnect(onReconnect)

  // When the server last answered with the value currently in `live`. Written
  // in a ref rather than state: it is metadata about a render, not a trigger
  // for one, and setting state here would loop on every poll.
  const liveAtRef = useRef<number | null>(null)
  const lastLiveRef = useRef<T | null>(null)

  const trustLive = online && !liveFailed && live !== null && live !== undefined
  if (trustLive && live !== lastLiveRef.current) {
    lastLiveRef.current = live as T
    liveAtRef.current = Date.now()
  }

  // Mirror to the device only on a genuinely live value. `key` and `userId` are
  // in the deps so a user switch re-stamps ownership rather than leaving the
  // previous nurse's stamp on data now on screen for someone else.
  useEffect(() => {
    if (!trustLive || !userId) return
    writeCache(key, live as T, userId)
  }, [key, userId, live, trustLive])

  if (trustLive) {
    return { data: live as T, source: 'live', savedAt: liveAtRef.current, online, unreachable: false }
  }

  const unreachable = online && liveFailed

  // Connected and not failing, but no data yet: this is an ordinary load in
  // progress, NOT a degraded view. Serving the cache here would put an offline
  // label on a screen that is about to be live, which trains a nurse to ignore
  // the label — the one thing that must never happen to it. The panel's own
  // loading state covers this render.
  if (!unreachable && online) {
    return { data: null, source: 'none', savedAt: null, online, unreachable: false }
  }

  // The panel still holds what it fetched before the drop. Prefer it over the
  // cache — it is the same data (the cache was written from it) without a JSON
  // parse, and if the cache write failed on quota it is the only copy left.
  const inMemory = lastLiveRef.current
  if (inMemory !== null && inMemory !== undefined) {
    return { data: inMemory, source: 'cache', savedAt: liveAtRef.current, online, unreachable }
  }

  // Cold start with no connection: the nurse opened Orb in the dead zone. This
  // is the read the cache exists for. readCache enforces ownership and age, so
  // a null here means "nothing this nurse may see", not merely "nothing".
  if (userId) {
    const hit = readCache<T>(key, userId)
    if (hit) return { data: hit.data, source: 'cache', savedAt: hit.savedAt, online, unreachable }
  }

  return { data: null, source: 'none', savedAt: null, online, unreachable }
}
