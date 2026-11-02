/**
 * The consumer side of the PHI cache.
 *
 * phiCache's own tests prove the bounds (ownership, age, size). These prove the
 * thing a nurse actually experiences: that data on screen is either LIVE or
 * LABELLED, never live-looking-but-old. The dangerous case is not the empty
 * screen — it is the screen that still shows the last board it fetched, with no
 * hint that the wi-fi died twenty minutes ago.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useOfflineSnapshot } from './useOfflineSnapshot'
import { CACHE_MAX_AGE_MS, readCache, writeCache, OFFLINE_CACHE_PREFIX } from '../lib/offline/phiCache'

const NURSE_A = 'U-NURSE-A'

function setOnline(value: boolean) {
  Object.defineProperty(navigator, 'onLine', { value, configurable: true })
  act(() => { window.dispatchEvent(new Event(value ? 'online' : 'offline')) })
}

beforeEach(() => {
  localStorage.clear()
  setOnline(true)
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  setOnline(true)
})

describe('useOfflineSnapshot — while online', () => {
  it('passes live data straight through and mirrors it to the cache', () => {
    const board = { patients: [{ name: 'Asha' }] }
    const { result } = renderHook(() => useOfflineSnapshot('nurse:board', board, { userId: NURSE_A }))

    expect(result.current.source).toBe('live')
    expect(result.current.data).toBe(board)
    expect(readCache<typeof board>('nurse:board', NURSE_A)!.data).toEqual(board)
  })

  it('never writes a null/undefined live value over a good cache', () => {
    writeCache('nurse:board', { patients: [{ name: 'Asha' }] }, NURSE_A)
    renderHook(() => useOfflineSnapshot<{ patients: unknown[] }>('nurse:board', null, { userId: NURSE_A }))
    expect(readCache<{ patients: unknown[] }>('nurse:board', NURSE_A)!.data.patients).toHaveLength(1)
  })

  it('writes nothing when nobody is signed in', () => {
    // Held outside the render, as a panel's state is: `live` must keep its
    // identity between renders (see the hook).
    const board = { patients: [] }
    renderHook(() => useOfflineSnapshot('nurse:board', board, { userId: null }))
    expect(localStorage.getItem(`${OFFLINE_CACHE_PREFIX}nurse:board`)).toBeNull()
  })
})

describe('useOfflineSnapshot — while offline', () => {
  it('marks data the panel still holds in memory as NOT live', () => {
    const board = { patients: [{ name: 'Asha' }] }
    const { result, rerender } = renderHook(
      ({ live }: { live: typeof board | null }) =>
        useOfflineSnapshot('nurse:board', live, { userId: NURSE_A }),
      { initialProps: { live: board as typeof board | null } },
    )
    expect(result.current.source).toBe('live')

    // Wi-fi drops. The panel's own state still holds the board it fetched.
    setOnline(false)
    rerender({ live: board })

    expect(result.current.source).toBe('cache')
    expect(result.current.data).toEqual(board)
    expect(result.current.savedAt).toBeGreaterThan(0)
  })

  it('serves the cache to a panel that has no data of its own (cold start offline)', () => {
    writeCache('nurse:board', { patients: [{ name: 'Asha' }] }, NURSE_A, Date.now() - 20 * 60_000)
    setOnline(false)

    const { result } = renderHook(() =>
      useOfflineSnapshot<{ patients: { name: string }[] }>('nurse:board', null, { userId: NURSE_A }),
    )

    expect(result.current.source).toBe('cache')
    expect(result.current.data!.patients[0].name).toBe('Asha')
    expect(Date.now() - result.current.savedAt!).toBeGreaterThanOrEqual(20 * 60_000)
  })

  it('reports nothing rather than something when there is no cache at all', () => {
    setOnline(false)
    const { result } = renderHook(() => useOfflineSnapshot('nurse:board', null, { userId: NURSE_A }))
    expect(result.current.source).toBe('none')
    expect(result.current.data).toBeNull()
  })

  it('refuses the previous nurse cache to the next nurse', () => {
    writeCache('nurse:board', { patients: [{ name: 'Asha' }] }, NURSE_A)
    setOnline(false)
    const { result } = renderHook(() =>
      useOfflineSnapshot('nurse:board', null, { userId: 'U-NURSE-B' }),
    )
    expect(result.current.source).toBe('none')
    expect(result.current.data).toBeNull()
  })

  it('does not overwrite the cache with data it is only replaying', () => {
    const cachedAt = Date.now() - 45 * 60_000
    writeCache('nurse:board', { patients: [{ name: 'Asha' }] }, NURSE_A, cachedAt)
    setOnline(false)
    renderHook(() =>
      useOfflineSnapshot<{ patients: unknown[] }>('nurse:board', null, { userId: NURSE_A }),
    )
    // savedAt must still be the moment the server actually answered.
    expect(readCache('nurse:board', NURSE_A)!.savedAt).toBe(cachedAt)
  })
})

describe('useOfflineSnapshot — server unreachable while the NIC says online', () => {
  it('labels the fallback rather than showing a stale board as live', () => {
    const board = { patients: [{ name: 'Asha' }] }
    const { result, rerender } = renderHook(
      ({ failed }: { failed: boolean }) =>
        useOfflineSnapshot('nurse:board', board, { userId: NURSE_A, liveFailed: failed }),
      { initialProps: { failed: false } },
    )
    expect(result.current.source).toBe('live')

    rerender({ failed: true })
    expect(result.current.source).toBe('cache')
    expect(result.current.unreachable).toBe(true)
    expect(result.current.online).toBe(true)
  })
})

describe('useOfflineSnapshot — reconnect', () => {
  it('asks the panel to refresh the moment the connection returns', () => {
    const onReconnect = vi.fn()
    setOnline(false)
    renderHook(() => useOfflineSnapshot('nurse:board', null, { userId: NURSE_A, onReconnect }))
    expect(onReconnect).not.toHaveBeenCalled()

    setOnline(true)
    expect(onReconnect).toHaveBeenCalledTimes(1)
  })

  it('clears the offline label once fresh data lands', () => {
    const stale = { patients: [{ name: 'Asha' }] }
    const fresh = { patients: [{ name: 'Asha' }, { name: 'Ravi' }] }
    const { result, rerender } = renderHook(
      ({ live }: { live: typeof stale }) => useOfflineSnapshot('nurse:board', live, { userId: NURSE_A }),
      { initialProps: { live: stale } },
    )
    setOnline(false)
    rerender({ live: stale })
    expect(result.current.source).toBe('cache')

    setOnline(true)
    rerender({ live: fresh })
    expect(result.current.source).toBe('live')
    expect(result.current.data).toBe(fresh)
  })
})

describe('useOfflineSnapshot — memory obeys the cache bounds', () => {
  it('does not reuse one patient snapshot when the cache key changes offline', () => {
    const a = { name: 'Patient A' }
    const b = { name: 'Patient B' }
    writeCache('chart:B', b, NURSE_A)
    const { result, rerender } = renderHook(
      ({ key, live }: { key: string; live: typeof a | null }) =>
        useOfflineSnapshot(key, live, { userId: NURSE_A }),
      { initialProps: { key: 'chart:A', live: a as typeof a | null } },
    )
    setOnline(false)
    rerender({ key: 'chart:B', live: null })
    expect(result.current.data).toEqual(b)
  })

  it('does not lend in-memory PHI to a different owner or a signed-out screen', () => {
    const board = { name: 'Patient A' }
    const { result, rerender } = renderHook(
      ({ owner }: { owner: string | null }) =>
        useOfflineSnapshot('board', board, { userId: owner }),
      { initialProps: { owner: NURSE_A as string | null } },
    )
    setOnline(false)
    rerender({ owner: 'U-NURSE-B' })
    expect(result.current.data).toBeNull()
    rerender({ owner: null })
    expect(result.current.data).toBeNull()
  })

  it('does not re-stamp the old owner payload while online before new data arrives', () => {
    const board = { name: 'Patient A' }
    const { result, rerender } = renderHook(
      ({ owner }: { owner: string }) => useOfflineSnapshot('board', board, { userId: owner }),
      { initialProps: { owner: NURSE_A } },
    )
    rerender({ owner: 'U-NURSE-B' })
    expect(result.current.data).toBeNull()
    expect(localStorage.getItem(`${OFFLINE_CACHE_PREFIX}board`)).toContain(NURSE_A)
  })

  it('expires an in-memory copy after one shift without user interaction', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-10T08:00:00Z'))
    const board = { name: 'Patient A' }
    const { result } = renderHook(() => useOfflineSnapshot('board', board, { userId: NURSE_A }))
    setOnline(false)
    expect(result.current.source).toBe('cache')
    act(() => { vi.advanceTimersByTime(CACHE_MAX_AGE_MS + 60_000) })
    expect(result.current.data).toBeNull()
    expect(result.current.source).toBe('none')
  })

  it('refuses an in-memory copy after the device clock moves backwards', () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-10-10T08:00:00Z'))
    const board = { name: 'Patient A' }
    const { result, rerender } = renderHook(() => useOfflineSnapshot('board', board, { userId: NURSE_A }))
    setOnline(false)
    vi.setSystemTime(new Date('2026-10-10T07:59:00Z'))
    rerender()
    expect(result.current.data).toBeNull()
  })

  it('honours an authoritative refusal even when offline memory exists', () => {
    const board = { name: 'Patient A' }
    const { result, rerender } = renderHook(
      ({ allowed }: { allowed: boolean }) =>
        useOfflineSnapshot('board', board, { userId: NURSE_A, cacheAllowed: allowed }),
      { initialProps: { allowed: true } },
    )
    setOnline(false)
    rerender({ allowed: false })
    expect(result.current.data).toBeNull()
    expect(result.current.source).toBe('none')
    expect(readCache('board', NURSE_A)).toBeNull()

    // Resetting a load error or toggling network state is not a fresh answer.
    rerender({ allowed: true })
    expect(result.current.data).toBeNull()
    setOnline(true)
    expect(result.current.data).toBeNull()
  })

  it('evicts an authoritative refusal so an offline remount cannot recover it', () => {
    writeCache('board', { name: 'Patient A' }, NURSE_A)
    writeCache('other', { name: 'Patient B' }, NURSE_A)
    const denied = renderHook(() =>
      useOfflineSnapshot('board', null, { userId: NURSE_A, cacheAllowed: false }),
    )
    denied.unmount()
    setOnline(false)
    const { result } = renderHook(() => useOfflineSnapshot('board', null, { userId: NURSE_A }))
    expect(result.current.source).toBe('none')
    expect(readCache('other', NURSE_A)).not.toBeNull()
  })

  it('keeps a refused chart blocked across remounts when only deletion is denied', () => {
    const key = 'chart:deletion-denied'
    const fullKey = OFFLINE_CACHE_PREFIX + key
    writeCache(key, { name: 'Old chart' }, NURSE_A)
    writeCache('other:deletion-denied', { name: 'Other chart' }, NURSE_A)
    const remove = Storage.prototype.removeItem
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(function (this: Storage, storedKey: string) {
      if (storedKey === fullKey) throw new DOMException('Deletion denied', 'SecurityError')
      remove.call(this, storedKey)
    })
    const denied = renderHook(() => useOfflineSnapshot(key, null, { userId: NURSE_A, cacheAllowed: false }))
    denied.unmount()
    expect(localStorage.getItem(fullKey)).not.toBeNull()
    setOnline(false)
    const cold = renderHook(() => useOfflineSnapshot(key, null, { userId: NURSE_A }))
    expect(cold.result.current.source).toBe('none')
    expect(readCache('other:deletion-denied', NURSE_A)).not.toBeNull()
    cold.unmount()

    setOnline(true)
    const fresh = { name: 'Freshly authorized chart' }
    const authorized = renderHook(() => useOfflineSnapshot(key, fresh, { userId: NURSE_A }))
    authorized.unmount()
    setOnline(false)
    const reopened = renderHook(() => useOfflineSnapshot(key, null, { userId: NURSE_A }))
    expect(reopened.result.current.source).toBe('cache')
    expect(reopened.result.current.data).toEqual(fresh)
  })

  it('accepts a new live answer after a refusal without reviving the denied payload', () => {
    const oldBoard = { name: 'Patient A' }
    const freshBoard = { name: 'Patient A', revised: true }
    const { result, rerender } = renderHook(
      ({ live, allowed }) => useOfflineSnapshot('board', live, { userId: NURSE_A, cacheAllowed: allowed }),
      { initialProps: { live: oldBoard, allowed: true } },
    )
    rerender({ live: oldBoard, allowed: false })
    rerender({ live: oldBoard, allowed: true })
    expect(result.current.data).toBeNull()
    rerender({ live: freshBoard, allowed: true })
    expect(result.current.data).toBe(freshBoard)
    expect(readCache('board', NURSE_A)?.data).toEqual(freshBoard)
  })
})
