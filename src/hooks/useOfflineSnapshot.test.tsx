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
import { readCache, writeCache, OFFLINE_CACHE_PREFIX } from '../lib/offline/phiCache'

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
