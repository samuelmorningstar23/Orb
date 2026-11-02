import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { clearOfflineQueue, clearToken, getUser, processOfflineQueue, sendMessage, setUser } from './client'

const QUEUE = 'orb_offline_queue'
const user = { id: 'NUR-A', name: 'Nurse A', role: 'nurse' }
const queued = (id: string, owner = user.id) => ({
  id, url: '/relay/channels/ward/messages', method: 'POST',
  body: { content: 'Review requested' }, timestamp: Date.now(),
  userId: owner, idempotencyKey: `key-${id}`,
})
const success = () => new Response('{}', { headers: { 'Content-Type': 'application/json' } })

beforeEach(() => {
  localStorage.clear()
  Object.defineProperty(navigator, 'onLine', { configurable: true, value: true })
  vi.stubGlobal('fetch', vi.fn(async () => success()))
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  localStorage.clear()
  delete (navigator as { onLine?: boolean }).onLine
})

describe('browser persistence boundaries', () => {
  it.each(['{broken', 'null', '[]', '{}', '{"id":7,"name":"A","role":"nurse"}'])(
    'treats damaged stored identity %s as signed out', raw => {
      localStorage.setItem('orb_user', raw)
      expect(getUser()).toBeNull()
      expect(localStorage.getItem('orb_user')).toBeNull()
    },
  )

  it('keeps compatible user metadata and nullable roles', () => {
    const saved = { ...user, role: null, authMethod: 'pin', facilities: [{ code: 'MAIN' }] }
    localStorage.setItem('orb_user', JSON.stringify(saved))
    expect(getUser()).toEqual(saved)
  })

  it('still lets sign-in and sign-out finish when browser storage is denied', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new DOMException('Denied') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('Denied') })
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new DOMException('Denied') })
    expect(getUser()).toBeNull()
    expect(() => setUser(user)).not.toThrow()
    expect(() => clearToken()).not.toThrow()
    expect(() => clearOfflineQueue()).not.toThrow()
  })

  it.each(['{broken', 'null', '{}'])(
    'does not strand replay after a damaged queue %s', async raw => {
      setUser(user)
      localStorage.setItem(QUEUE, raw)
      await expect(processOfflineQueue()).resolves.toBeUndefined()
      localStorage.setItem(QUEUE, JSON.stringify([queued('valid')]))
      await processOfflineQueue()
      expect(fetch).toHaveBeenCalledTimes(1)
      expect(JSON.parse(localStorage.getItem(QUEUE)!)).toEqual([])
    },
  )

  it('preserves valid neighbours and legacy envelopes without an idempotency key', async () => {
    setUser(user)
    const legacy = { ...queued('legacy'), body: '{"content":"Legacy clinical note"}' }
    const { idempotencyKey: _unused, ...withoutKey } = legacy
    localStorage.setItem(QUEUE, JSON.stringify([null, {}, queued('valid'), withoutKey]))
    await processOfflineQueue()
    expect(fetch).toHaveBeenCalledTimes(2)
    const calls = vi.mocked(fetch).mock.calls
    expect((calls[0][1]?.headers as Record<string, string>)['Idempotency-Key']).toBe('key-valid')
    expect(calls[1][1]?.body).toBe(withoutKey.body)
    expect(JSON.parse(localStorage.getItem(QUEUE)!)).toEqual([])
  })

  it('releases the replay lock if ownership cleanup cannot be persisted', async () => {
    setUser(user)
    localStorage.setItem(QUEUE, JSON.stringify([queued('foreign', 'NUR-B'), queued('mine')]))
    const write = vi.spyOn(Storage.prototype, 'setItem').mockImplementationOnce(() => {
      throw new DOMException('Storage full', 'QuotaExceededError')
    })
    await expect(processOfflineQueue()).rejects.toThrow('Storage full')
    write.mockRestore()
    await processOfflineQueue()
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(JSON.parse(localStorage.getItem(QUEUE)!)).toEqual([])
  })

  it('stops the remaining replay when another clinician signs in mid-request', async () => {
    setUser(user)
    const second = queued('second')
    const first = queued('first')
    localStorage.setItem(QUEUE, JSON.stringify([first, second]))
    let finish!: (response: Response) => void
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(resolve => { finish = resolve })))
    const replay = processOfflineQueue()
    expect(fetch).toHaveBeenCalledTimes(1)
    setUser({ id: 'NUR-B', name: 'Nurse B', role: 'nurse' })
    finish(success())
    await replay
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(JSON.parse(localStorage.getItem(QUEUE)!)).toEqual([first, second])
  })

  it.each([200, 401])('holds queued work and releases replay after identity changes during HTTP %s refresh', async status => {
    setUser(user)
    const pending = [queued('first'), queued('second')]
    localStorage.setItem(QUEUE, JSON.stringify(pending))
    let finish!: (response: Response) => void
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response('{}', { status: 401 }))
      .mockImplementationOnce(() => new Promise<Response>(resolve => { finish = resolve }))
    vi.stubGlobal('fetch', fetch)
    const expired = vi.fn()
    const failed = vi.fn()
    window.addEventListener('orb:session-expired', expired)
    window.addEventListener('orb:offline-sync-failed', failed)
    try {
      const replay = processOfflineQueue()
      await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
      const nextUser = { id: 'NUR-B', name: 'Nurse B', role: 'nurse' }
      setUser(nextUser)
      finish(new Response('{}', { status }))
      await replay

      expect(fetch).toHaveBeenCalledTimes(2)
      expect(JSON.parse(localStorage.getItem(QUEUE)!)).toEqual(pending)
      expect(getUser()).toEqual(nextUser)
      expect(expired).not.toHaveBeenCalled()
      expect(failed).not.toHaveBeenCalled()

      // The original author can explicitly return and replay; the lock was
      // released and each mutation retains its original idempotency key.
      setUser(user)
      fetch.mockImplementation(async () => success())
      await processOfflineQueue()
      expect(fetch).toHaveBeenCalledTimes(4)
      expect(JSON.parse(localStorage.getItem(QUEUE)!)).toEqual([])
    } finally {
      window.removeEventListener('orb:session-expired', expired)
      window.removeEventListener('orb:offline-sync-failed', failed)
    }
  })

  it('does not return an optimistic save if an offline write cannot be persisted', async () => {
    setUser(user)
    Object.defineProperty(navigator, 'onLine', { configurable: true, value: false })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('Storage full', 'QuotaExceededError')
    })
    await expect(sendMessage('ward', 'Review requested')).rejects.toThrow('Storage full')
    expect(fetch).not.toHaveBeenCalled()
  })
})
