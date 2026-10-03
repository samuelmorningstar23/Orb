/**
 * THE OTHER PHI STORE ON A SHARED TABLET.
 *
 * lib/offline/phiCache is the cache a nurse READS from offline, and it is
 * stamped, aged and wiped on every handover. This file is about the other half,
 * which had none of that: api/client keeps an offline WRITE queue in
 * localStorage under 'orb_offline_queue' — free clinical text, patient ids and
 * dose payloads — and it is the store nobody was clearing.
 *
 * Two failures, one shape (the queue does not know whose it is):
 *
 *   LEAK            nurse A signs out mid-round with a queued note about a named
 *                   patient. clearToken() removes the token and the user; the
 *                   queue stays. Nurse B picks the tablet up and A's clinical
 *                   text is still on the device.
 *   MISATTRIBUTION  worse, and silent: the replay fires on the next 'online'
 *                   event, whoever is signed in by then. The request goes out on
 *                   nurse B's session cookie, so the server records B as the
 *                   author of A's note. Nobody is told, and the chart is wrong
 *                   in a way no one can see afterwards.
 *
 * So a queued write is stamped with its author and is replayed ONLY for that
 * author — the same ownership rule phiCache already applies to reads.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { clearOfflineQueue, processOfflineQueue, sendMessage } from './client'

const QUEUE_KEY = 'orb_offline_queue'
const NURSE_A = 'NUR-A'
const NURSE_B = 'NUR-B'

function signIn(id: string) {
  localStorage.setItem('orb_user', JSON.stringify({ id, name: id, role: 'nurse' }))
}

function setOnline(value: boolean) {
  Object.defineProperty(navigator, 'onLine', { value, configurable: true })
}

function readQueue(): Array<Record<string, unknown>> {
  return JSON.parse(localStorage.getItem(QUEUE_KEY) ?? '[]')
}

/** One queued write, as the client persists it. */
function seedQueue(userId: string | null) {
  localStorage.setItem(QUEUE_KEY, JSON.stringify([{
    id: 'req-1',
    url: '/vigil/patients/P001/notes',
    method: 'POST',
    body: { content: 'Chest clear, for discharge tomorrow', doc_type: 'Progress Note' },
    timestamp: Date.now(),
    idempotencyKey: 'key-1',
    ...(userId === null ? {} : { userId }),
  }]))
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  localStorage.clear()
  setOnline(true)
  fetchMock = vi.fn(async () => ({
    ok: true, status: 200,
    json: async () => ({ ok: true }),
    headers: { get: () => null },
  }))
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
})

describe('the offline write queue knows whose it is', () => {
  it('stamps the nurse who queued the write', async () => {
    signIn(NURSE_A)
    setOnline(false)

    await sendMessage('ward-general', 'Bed 4 needs a review')

    expect(fetchMock).not.toHaveBeenCalled()
    expect(readQueue()).toHaveLength(1)
    expect(readQueue()[0].userId).toBe(NURSE_A)
  })

  it('replays the signed-in nurse her own queued write', async () => {
    seedQueue(NURSE_A)
    signIn(NURSE_A)

    await processOfflineQueue()

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(readQueue()).toHaveLength(0)
  })

  it('never replays the previous nurse\'s write under this nurse\'s session', async () => {
    // Nurse A's tablet died with a note queued. Nurse B signed in on it and
    // walked back into wi-fi range.
    seedQueue(NURSE_A)
    signIn(NURSE_B)

    const failures: unknown[] = []
    const listener = (e: Event) => failures.push((e as CustomEvent).detail)
    window.addEventListener('orb:offline-sync-failed', listener)
    await processOfflineQueue()
    window.removeEventListener('orb:offline-sync-failed', listener)

    // Nothing went to the server as Nurse B...
    expect(fetchMock).not.toHaveBeenCalled()
    // ...the PHI is off the device...
    expect(readQueue()).toHaveLength(0)
    // ...and it was surfaced rather than silently binned.
    expect(failures).toHaveLength(1)
  })

  it('refuses a write queued by an older build that carries no author', async () => {
    seedQueue(null)
    signIn(NURSE_A)

    await processOfflineQueue()

    expect(fetchMock).not.toHaveBeenCalled()
    expect(readQueue()).toHaveLength(0)
  })

  it('holds a queued write when nobody is signed in, rather than firing it blind', async () => {
    // The 'online' event can land on the login screen. There is no identity to
    // attribute the write to and nobody to warn, so it waits — the sign-out and
    // sign-in paths clear it, which is where that decision belongs.
    seedQueue(NURSE_A)

    await processOfflineQueue()

    expect(fetchMock).not.toHaveBeenCalled()
    expect(readQueue()).toHaveLength(1)
  })

  it('empties on demand, for the sign-out path to call', () => {
    seedQueue(NURSE_A)
    expect(clearOfflineQueue()).toBe(1)
    expect(localStorage.getItem(QUEUE_KEY)).toBeNull()
  })
})
