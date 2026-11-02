/**
 * The on-device cache for offline reading.
 *
 * This holds PHI on a tablet that lives on a trolley and passes between six
 * nurses a shift, so the interesting part is not the caching — it is the
 * bounds. Four of them, each closing a way this leaks or misleads:
 *
 *   OWNERSHIP  every entry is stamped with the user who wrote it, and a read by
 *              anyone else WIPES THE DEVICE rather than merely missing. A nurse
 *              handing the tablet on must not leave her patients behind.
 *   AGE        an entry older than a shift is refused. Stale vitals shown as
 *              current are worse than no vitals.
 *   SIZE       bounded and evicted oldest-first, so the cache cannot grow until
 *              the browser starts throwing quota errors mid-round.
 *   HONESTY    a read returns how OLD the data is, so the UI can say so. The
 *              caller cannot accidentally present cached data as live.
 *
 * localStorage is the store: synchronous (so a render can decide what to show
 * without a loading flash) and universally available. It is NOT encrypted —
 * which is exactly why the age and wipe rules above are tight.
 */

/** Every key this module owns starts with this, so clearing is exact and
 *  cannot take a theme preference or a draft with it. */
export const OFFLINE_CACHE_PREFIX = 'orb:offline:'

/** One shift. Past this, a cached chart is refused rather than shown stale. */
export const CACHE_MAX_AGE_MS = 12 * 60 * 60 * 1000

/** Total cache ceiling. localStorage is typically ~5 MB per origin; staying
 *  well under it leaves room for everything else Orb stores. */
export const CACHE_MAX_BYTES = 2_000_000

// A refusal remains authoritative for this running app even when the browser
// lets us read storage but denies deletion. This survives component remounts;
// only a successfully persisted fresh response releases the block. It is not
// a revocation record across browser reloads or other tabs.
const refusedKeys = new Set<string>()

interface CacheEnvelope<T> {
  data: T
  savedAt: number
  /** The user this entry belongs to. An entry WITHOUT this was written by an
   *  older build that had no ownership rule; it is refused, never guessed at. */
  userId?: string
}

export interface CacheHit<T> {
  data: T
  /** How long ago this was cached. The UI must show it — that is the whole
   *  point of returning it rather than just the data. */
  ageMs: number
  savedAt: number
}

function storage(): Storage | null {
  try {
    if (typeof localStorage === 'undefined') return null
    return localStorage
  } catch {
    // Safari in private mode throws on access rather than returning null.
    return null
  }
}

function listCacheKeys(store: Storage): string[] {
  const keys: string[] = []
  for (let i = 0; i < store.length; i++) {
    const key = store.key(i)
    if (key && key.startsWith(OFFLINE_CACHE_PREFIX)) keys.push(key)
  }
  return keys
}

function cacheKeys(): string[] {
  const store = storage()
  if (!store) return []
  try {
    return listCacheKeys(store)
  } catch {
    return []
  }
}

function removeStoredKey(store: Storage, key: string): boolean {
  try {
    store.removeItem(key)
    return true
  } catch {
    return false
  }
}

/** Evict an authoritative refusal without deleting other charts or drafts. */
export function removeCache(key: string): boolean {
  const fullKey = OFFLINE_CACHE_PREFIX + key
  refusedKeys.add(fullKey)
  const store = storage()
  return store ? removeStoredKey(store, fullKey) : false
}

function readEnvelope<T>(key: string, forEviction = false): CacheEnvelope<T> | null {
  if (!forEviction && refusedKeys.has(key)) return null
  const store = storage()
  if (!store) return null
  try {
    const raw = store.getItem(key)
    if (!raw) return null
    const parsed = JSON.parse(raw) as CacheEnvelope<T>
    if (!parsed || typeof parsed !== 'object' || !Number.isFinite(parsed.savedAt)) {
      removeStoredKey(store, key)
      return null
    }
    return parsed
  } catch {
    // Corrupt entry — a half-written record from a tab killed mid-write.
    // Drop it rather than throwing into a render.
    removeStoredKey(store, key)
    return null
  }
}

/**
 * Cache a value for one user.
 *
 * @returns false when nothing was stored — no signed-in user, no storage, or a
 *          value larger than the entire cap. Callers should treat false as
 *          "offline reading will not work for this", not as a silent success.
 */
export function writeCache<T>(
  key: string,
  data: T,
  userId: string,
  savedAt: number = Date.now(),
): boolean {
  const store = storage()
  // No signed-in user means no owner to stamp, and an unowned entry is exactly
  // what the ownership rule exists to refuse. Do not write one.
  if (!store || !userId || !Number.isFinite(savedAt)) return false

  const envelope: CacheEnvelope<T> = { data, savedAt, userId }
  let serialised: string
  try {
    serialised = JSON.stringify(envelope)
  } catch {
    return false // circular or non-serialisable
  }

  // A single value bigger than the whole cache can never be stored without
  // evicting everything and still overflowing. Refuse it outright rather than
  // emptying the cache for something that will not fit anyway.
  if (serialised.length > CACHE_MAX_BYTES) return false

  const fullKey = OFFLINE_CACHE_PREFIX + key
  try {
    store.setItem(fullKey, serialised)
  } catch {
    // Quota exceeded. Make room and try once more.
    if (!evictOldest(serialised.length)) return false
    try {
      store.setItem(fullKey, serialised)
    } catch {
      return false
    }
  }

  if (!evictOldest(0)) {
    removeStoredKey(store, fullKey)
    return false
  }
  refusedKeys.delete(fullKey)
  return true
}

/**
 * Read a cached value for one user.
 *
 * Returns null — and takes the entry away — whenever it cannot be trusted:
 * wrong owner, unstamped, expired, future-dated, or corrupt.
 */
export function readCache<T>(
  key: string,
  userId: string,
  now: number = Date.now(),
): CacheHit<T> | null {
  const store = storage()
  if (!store || !userId) return null

  const fullKey = OFFLINE_CACHE_PREFIX + key
  const envelope = readEnvelope<T>(fullKey)
  if (!envelope) return null

  // OWNERSHIP. An unstamped entry is refused rather than guessed at, and an
  // entry belonging to SOMEONE ELSE wipes the whole device — because if the
  // tablet has changed hands, every other entry on it belongs to the previous
  // nurse too. Hiding one entry while leaving the rest would be the leak.
  if (!envelope.userId) {
    removeStoredKey(store, fullKey)
    return null
  }
  if (envelope.userId !== userId) {
    clearOfflineCache()
    return null
  }

  // AGE. A future stamp means the clock moved (or the entry was tampered with);
  // its age is unknowable, so it cannot be honestly labelled.
  const ageMs = now - envelope.savedAt
  if (!Number.isFinite(ageMs) || ageMs < 0 || ageMs > CACHE_MAX_AGE_MS) {
    removeStoredKey(store, fullKey)
    return null
  }

  return { data: envelope.data, ageMs, savedAt: envelope.savedAt }
}

/**
 * Remove every cached entry. MUST be called on logout and on user switch —
 * that is the difference between a shared tablet and a PHI leak.
 *
 * @returns how many entries were removed.
 */
export function clearOfflineCache(): number {
  const store = storage()
  if (!store) return 0
  const keys = cacheKeys()
  return keys.reduce((removed, key) => removed + Number(removeStoredKey(store, key)), 0)
}

export function cacheEntryCount(): number {
  return cacheKeys().length
}

export function cacheBytes(): number {
  const store = storage()
  if (!store) return 0
  try {
    return cacheKeys().reduce((total, key) => total + (store.getItem(key)?.length ?? 0), 0)
  } catch {
    return 0
  }
}

/**
 * When this user's freshest cached data was saved, or null if they have none.
 * Used by the offline banner to say HOW OLD what you are looking at is.
 */
export function newestCachedAt(userId: string, now: number = Date.now()): number | null {
  const store = storage()
  if (!store || !userId) return null

  let newest: number | null = null
  for (const key of cacheKeys()) {
    const envelope = readEnvelope<unknown>(key)
    if (!envelope || envelope.userId !== userId) continue
    const ageMs = now - envelope.savedAt
    if (!Number.isFinite(ageMs) || ageMs < 0 || ageMs > CACHE_MAX_AGE_MS) continue
    if (newest === null || envelope.savedAt > newest) newest = envelope.savedAt
  }
  return newest
}

/**
 * Evict oldest-first until the cache fits under the cap.
 *
 * Oldest-first rather than least-recently-used on purpose: age is already the
 * trust boundary here, so the entry closest to expiry is the one with least
 * value left. It is also the only ordering we can compute without writing on
 * every read, which on a synchronous store would cost a render.
 */
function evictOldest(incomingBytes: number): boolean {
  const store = storage()
  if (!store) return false

  try {
    const entries = listCacheKeys(store)
      .map(key => {
        // Only size/age metadata is inspected here, never published. A fresh
        // replacement of a blocked key must keep its true eviction priority.
        const envelope = readEnvelope<unknown>(key, true)
        return {
          key,
          savedAt: envelope?.savedAt ?? 0,
          bytes: store.getItem(key)?.length ?? 0,
        }
      })
      .sort((a, b) => a.savedAt - b.savedAt)

    let total = entries.reduce((sum, e) => sum + e.bytes, 0) + incomingBytes
    for (const entry of entries) {
      if (total <= CACHE_MAX_BYTES) break
      if (!removeStoredKey(store, entry.key)) return false
      total -= entry.bytes
    }
    return true
  } catch {
    return false
  }
}
