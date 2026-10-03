/**
 * The device cache holds PHI. These tests are the bounds on it, not a
 * round-trip smoke test: who can read it, how long it lives, how big it gets,
 * and what a handover between two nurses on one tablet is allowed to leak.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import {
  writeCache,
  readCache,
  clearOfflineCache,
  cacheEntryCount,
  cacheBytes,
  newestCachedAt,
  OFFLINE_CACHE_PREFIX,
  CACHE_MAX_AGE_MS,
  CACHE_MAX_BYTES,
} from './phiCache'

const NURSE_A = 'U-NURSE-A'
const NURSE_B = 'U-NURSE-B'

beforeEach(() => {
  localStorage.clear()
})

describe('phiCache — ownership', () => {
  it('round-trips a value for the user who wrote it', () => {
    writeCache('board', { patients: [{ name: 'Asha' }] }, NURSE_A)
    const hit = readCache<{ patients: { name: string }[] }>('board', NURSE_A)
    expect(hit).not.toBeNull()
    expect(hit!.data.patients[0].name).toBe('Asha')
    expect(hit!.ageMs).toBeGreaterThanOrEqual(0)
  })

  it('refuses the previous nurse cache to the next nurse AND wipes the device', () => {
    writeCache('board', { patients: [{ name: 'Asha' }] }, NURSE_A)
    expect(cacheEntryCount()).toBe(1)

    // Nurse B picks up the same ward tablet.
    expect(readCache('board', NURSE_B)).toBeNull()
    // Not just hidden — gone. A later re-read as A must not resurrect it.
    expect(cacheEntryCount()).toBe(0)
    expect(readCache('board', NURSE_A)).toBeNull()
  })

  it('refuses an unstamped entry (written by an older build) rather than guessing', () => {
    localStorage.setItem(
      `${OFFLINE_CACHE_PREFIX}board`,
      JSON.stringify({ data: { patients: [] }, savedAt: Date.now() }),
    )
    expect(readCache('board', NURSE_A)).toBeNull()
  })

  it('refuses everything when nobody is signed in', () => {
    writeCache('board', { patients: [] }, NURSE_A)
    expect(readCache('board', '')).toBeNull()
    expect(writeCache('board2', { patients: [] }, '')).toBe(false)
  })

  it('survives a corrupt entry without throwing', () => {
    localStorage.setItem(`${OFFLINE_CACHE_PREFIX}board`, '{not json')
    expect(readCache('board', NURSE_A)).toBeNull()
  })
})

describe('phiCache — age bound', () => {
  it('serves an entry inside one shift', () => {
    const now = Date.now()
    writeCache('board', { n: 1 }, NURSE_A, now - 60_000)
    const hit = readCache<{ n: number }>('board', NURSE_A, now)
    expect(hit!.data.n).toBe(1)
    expect(hit!.ageMs).toBe(60_000)
  })

  it('refuses an entry older than one shift and drops it', () => {
    const now = Date.now()
    writeCache('board', { n: 1 }, NURSE_A, now - CACHE_MAX_AGE_MS - 1)
    expect(readCache('board', NURSE_A, now)).toBeNull()
    expect(cacheEntryCount()).toBe(0)
  })

  it('refuses an entry stamped in the future (clock moved) rather than trusting it', () => {
    const now = Date.now()
    writeCache('board', { n: 1 }, NURSE_A, now + 5 * 60_000)
    expect(readCache('board', NURSE_A, now)).toBeNull()
  })
})

describe('phiCache — size bound', () => {
  it('evicts the oldest entries to stay under the byte cap', () => {
    const now = Date.now()
    const big = 'x'.repeat(200_000)
    // 12 × ~200 KB = ~2.4 MB, over the 2 MB cap.
    for (let i = 0; i < 12; i++) {
      writeCache(`chunk:${i}`, { big }, NURSE_A, now + i)
    }
    expect(cacheBytes()).toBeLessThanOrEqual(CACHE_MAX_BYTES)
    // The newest survives, the oldest does not.
    expect(readCache('chunk:11', NURSE_A, now + 12)).not.toBeNull()
    expect(readCache('chunk:0', NURSE_A, now + 12)).toBeNull()
  })

  it('refuses to store a single value larger than the whole cap', () => {
    const huge = 'x'.repeat(CACHE_MAX_BYTES + 10)
    expect(writeCache('huge', { huge }, NURSE_A)).toBe(false)
    expect(cacheEntryCount()).toBe(0)
  })
})

describe('phiCache — clearing', () => {
  it('clearOfflineCache removes every cache key and touches nothing else', () => {
    writeCache('board', { n: 1 }, NURSE_A)
    writeCache('vitals:P1', { n: 2 }, NURSE_A)
    localStorage.setItem('orb_theme', 'dark')
    localStorage.setItem('orb:nurse:obs-draft:P1', '{}')

    const removed = clearOfflineCache()

    expect(removed).toBe(2)
    expect(cacheEntryCount()).toBe(0)
    expect(localStorage.getItem('orb_theme')).toBe('dark')
    expect(localStorage.getItem('orb:nurse:obs-draft:P1')).toBe('{}')
  })
})

describe('phiCache — newestCachedAt', () => {
  it('is null with nothing cached', () => {
    expect(newestCachedAt(NURSE_A)).toBeNull()
  })

  it('reports the freshest entry belonging to this user', () => {
    const now = Date.now()
    writeCache('a', { n: 1 }, NURSE_A, now - 600_000)
    writeCache('b', { n: 2 }, NURSE_A, now - 120_000)
    expect(newestCachedAt(NURSE_A, now)).toBe(now - 120_000)
  })

  it('ignores another user entries', () => {
    const now = Date.now()
    writeCache('a', { n: 1 }, NURSE_B, now - 120_000)
    expect(newestCachedAt(NURSE_A, now)).toBeNull()
  })
})
