import { useEffect, useState } from 'react'
import { CACHE_MAX_AGE_MS, readCache, removeCache, writeCache } from '../lib/offline/phiCache'
import { getUser } from '../api/client'
import { useOfflineStatus, useOnReconnect } from './useOfflineStatus'
import { useClockAtRender, useNow } from './useNow'

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
  /** A server refusal is authoritative, even if this device has an older copy.
   *  Set false for authorization/not-found failures; only network/server outages
   *  should fall back to previously authorized cached data. */
  cacheAllowed?: boolean
  /** Called when the connection returns, so the panel can refetch immediately
   *  rather than waiting out its poll interval. */
  onReconnect?: () => void
}

/** `live` must keep its identity between renders: pass the panel's React state
 *  (or null), never an object built during the render. A new object on every
 *  render reads as a new server answer every render, and the hook would re-stamp
 *  it forever (React stops that with "Too many re-renders"). */
export function useOfflineSnapshot<T>(
  key: string,
  live: T | null | undefined,
  options: OfflineSnapshotOptions = {},
): OfflineSnapshot<T> {
  const { liveFailed = false, cacheAllowed = true, onReconnect } = options
  const userId = options.userId !== undefined ? options.userId : getUser()?.id ?? null

  const { online } = useOfflineStatus()
  useOnReconnect(onReconnect)
  // Re-evaluate the shift limit while a copy remains on screen, not just when
  // another fetch or user interaction happens to render the panel again.
  const now = useNow(60_000, !online || liveFailed)

  // The last LIVE value and when the server answered with it. State adjusted
  // DURING render when a new live value arrives, which is React's pattern for
  // remembering something about earlier renders. It used to be two refs written
  // during render, which React may replay or throw away (react-hooks/refs).
  // The identity guard makes it one extra render per new live value, not a loop
  // on every poll: every caller passes React state, so `live` keeps its identity
  // until the panel really stores a new answer.
  const renderedAt = useClockAtRender()
  const [stamp, setStamp] = useState<{ key: string; userId: string | null; value: T; at: number; invalidated?: boolean } | null>(null)

  const sameScope = stamp?.key === key && stamp.userId === userId
  const ownStamp = sameScope && !stamp.invalidated ? stamp : null
  // When the owner or chart changes, the previous value is not a new answer
  // merely because its props changed. Wait for a genuinely different live
  // payload before publishing it under the new cache key or clinician. An
  // authoritative refusal also invalidates the last payload: resetting an
  // error or changing network state cannot make that old answer live again.
  const previousScopeValue = stamp !== null && ownStamp === null && live === stamp.value
  const trustLive = cacheAllowed && online && !liveFailed && live !== null && live !== undefined && !previousScopeValue
  const freshLive = trustLive && live !== stamp?.value
  if (freshLive) setStamp({ key, userId, value: live as T, at: renderedAt })
  if (!cacheAllowed && sameScope && !stamp.invalidated) setStamp({ ...stamp, invalidated: true })
  if (!cacheAllowed && !sameScope && live !== null && live !== undefined) {
    setStamp({ key, userId, value: live, at: renderedAt, invalidated: true })
  }
  const lastLive = freshLive ? { key, userId, value: live as T, at: renderedAt } : ownStamp

  // Mirror only a genuinely live value for this scope. A key/owner change
  // reruns the guarded effect; it cannot re-stamp the previous payload as a
  // fresh answer for another chart or clinician.
  useEffect(() => {
    if (!cacheAllowed) {
      removeCache(key)
      return
    }
    if (!trustLive || !userId) return
    writeCache(key, live as T, userId)
  }, [key, userId, live, trustLive, cacheAllowed])

  if (trustLive) {
    return { data: live as T, source: 'live', savedAt: lastLive?.at ?? null, online, unreachable: false }
  }

  const unreachable = online && liveFailed

  if (!cacheAllowed) {
    return { data: null, source: 'none', savedAt: null, online, unreachable: false }
  }

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
  if (lastLive && userId && now >= lastLive.at && now - lastLive.at <= CACHE_MAX_AGE_MS) {
    return { data: lastLive.value, source: 'cache', savedAt: lastLive.at, online, unreachable }
  }

  // Cold start with no connection: the nurse opened Orb in the dead zone. This
  // is the read the cache exists for. readCache enforces ownership and age, so
  // a null here means "nothing this nurse may see", not merely "nothing".
  if (userId) {
    const hit = readCache<T>(key, userId, now)
    if (hit) return { data: hit.data, source: 'cache', savedAt: hit.savedAt, online, unreachable }
  }

  return { data: null, source: 'none', savedAt: null, online, unreachable }
}
