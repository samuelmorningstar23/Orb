/**
 * The PHI-leak bound on a shared ward tablet.
 *
 * A trolley tablet passes between six nurses a shift. The offline cache is the
 * one thing in Orb that deliberately keeps a nurse's patients on the device, so
 * it is the one thing that must be gone the instant the session ends — and the
 * instant a DIFFERENT person signs in, which is not always preceded by a clean
 * sign-out (flat battery, force-closed browser, tablet snatched mid-round).
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useOfflineSession } from './useOfflineSession'
import { writeCache, cacheEntryCount } from '../lib/offline/phiCache'

const NURSE_A = 'U-NURSE-A'
const NURSE_B = 'U-NURSE-B'

beforeEach(() => { localStorage.clear() })
afterEach(() => { localStorage.clear() })

describe('useOfflineSession', () => {
  it('leaves the signed-in nurse her own cache', () => {
    writeCache('nurse:board', { patients: [{ name: 'Asha' }] }, NURSE_A)
    const { rerender } = renderHook(({ id }: { id: string | null }) => useOfflineSession(id), {
      initialProps: { id: NURSE_A as string | null },
    })
    rerender({ id: NURSE_A })
    expect(cacheEntryCount()).toBe(1)
  })

  it('wipes the device on logout', () => {
    const { rerender } = renderHook(({ id }: { id: string | null }) => useOfflineSession(id), {
      initialProps: { id: NURSE_A as string | null },
    })
    writeCache('nurse:board', { patients: [{ name: 'Asha' }] }, NURSE_A)
    expect(cacheEntryCount()).toBe(1)

    rerender({ id: null })
    expect(cacheEntryCount()).toBe(0)
  })

  it('wipes the device when the next nurse signs in without the last one signing out', () => {
    const { rerender } = renderHook(({ id }: { id: string | null }) => useOfflineSession(id), {
      initialProps: { id: NURSE_A as string | null },
    })
    writeCache('nurse:board', { patients: [{ name: 'Asha' }] }, NURSE_A)

    rerender({ id: NURSE_B })
    expect(cacheEntryCount()).toBe(0)
  })

  it('wipes a cache left behind by a crash, on the next mount under a new user', () => {
    // Nurse A's tablet died mid-round: no logout ran, the cache survived, and
    // the app is now booting fresh with Nurse B already signed in.
    writeCache('nurse:board', { patients: [{ name: 'Asha' }] }, NURSE_A)
    expect(cacheEntryCount()).toBe(1)

    renderHook(() => useOfflineSession(NURSE_B))
    expect(cacheEntryCount()).toBe(0)
  })

  it('wipes on mount when nobody is signed in at all', () => {
    writeCache('nurse:board', { patients: [{ name: 'Asha' }] }, NURSE_A)
    renderHook(() => useOfflineSession(null))
    expect(cacheEntryCount()).toBe(0)
  })

  it('keeps the cache across a re-mount for the SAME nurse (a page refresh mid-shift)', () => {
    writeCache('nurse:board', { patients: [{ name: 'Asha' }] }, NURSE_A)
    renderHook(() => useOfflineSession(NURSE_A))
    expect(cacheEntryCount()).toBe(1)
  })
})

/**
 * The offline WRITE queue (api/client, 'orb_offline_queue') is the second PHI
 * store on the device, and it was surviving every handover: free clinical text
 * and dose payloads left for the next nurse, then replayed on her session. The
 * cache and the queue must die together — one call site, one rule.
 */
describe('useOfflineSession and the offline write queue', () => {
  const QUEUE_KEY = 'orb_offline_queue'
  const queueOne = (userId: string) => localStorage.setItem(QUEUE_KEY, JSON.stringify([{
    id: 'r1', url: '/vigil/patients/P001/notes', method: 'POST',
    body: { content: 'For discharge tomorrow' }, timestamp: Date.now(), userId,
  }]))
  const queued = () => JSON.parse(localStorage.getItem(QUEUE_KEY) ?? '[]').length

  it('empties the queue on sign-out', () => {
    const { rerender } = renderHook(({ id }: { id: string | null }) => useOfflineSession(id), {
      initialProps: { id: NURSE_A as string | null },
    })
    queueOne(NURSE_A)

    rerender({ id: null })
    expect(queued()).toBe(0)
  })

  it('empties the queue when a different nurse signs in', () => {
    const { rerender } = renderHook(({ id }: { id: string | null }) => useOfflineSession(id), {
      initialProps: { id: NURSE_A as string | null },
    })
    queueOne(NURSE_A)

    rerender({ id: NURSE_B })
    expect(queued()).toBe(0)
  })

  it('empties a queue left behind by a crash, on the next boot under a new user', () => {
    // No cache entries at all — the tablet died between the write being queued
    // and anything being cached. The queue is still PHI and still replayable.
    queueOne(NURSE_A)
    renderHook(() => useOfflineSession(NURSE_B))
    expect(queued()).toBe(0)
  })

  it('leaves the queue alone for the nurse who owns it (a page refresh mid-shift)', () => {
    writeCache('nurse:board', { patients: [{ name: 'Asha' }] }, NURSE_A)
    queueOne(NURSE_A)
    renderHook(() => useOfflineSession(NURSE_A))
    expect(queued()).toBe(1)
  })
})
