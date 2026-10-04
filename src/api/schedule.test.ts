import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  fetchAvailability,
  fetchClinicSessions,
  isFeatureDisabled,
  updateClinicSession,
  withdrawSessionException,
} from './schedule'

const apiFetch = vi.fn()

vi.mock('./client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
}))

beforeEach(() => {
  apiFetch.mockReset()
  apiFetch.mockResolvedValue({})
})

describe('the clinic timings client', () => {
  it('leaves out empty filters and lets the server choose the hospital day', async () => {
    await fetchAvailability()
    expect(apiFetch).toHaveBeenLastCalledWith('/schedule/availability', undefined)
    await fetchAvailability({ date: '2026-10-05', days: 7, doctorId: 'DOC 1' })
    expect(apiFetch).toHaveBeenLastCalledWith(
      '/schedule/availability?date=2026-10-05&days=7&doctor_id=DOC+1', undefined)
    await fetchClinicSessions({ includeRetired: false })
    expect(apiFetch).toHaveBeenLastCalledWith('/schedule/sessions', undefined)
  })

  it('sends the row version with every edit and escapes ids in the path', async () => {
    await updateClinicSession('CS/1', 3, { room: 'Room 2' })
    const [path, options] = apiFetch.mock.calls.at(-1) as [string, RequestInit]
    expect(path).toBe('/schedule/sessions/CS%2F1')
    expect(options.method).toBe('PATCH')
    expect(JSON.parse(String(options.body))).toEqual({ room: 'Room 2', row_version: 3 })
    await withdrawSessionException('CX1', 'Back early')
    expect(apiFetch).toHaveBeenLastCalledWith('/schedule/exceptions/CX1/withdraw',
      { method: 'POST', body: JSON.stringify({ reason: 'Back early' }) })
  })

  it('recognises the switched-off answer and nothing else', () => {
    expect(isFeatureDisabled({ status: 404, detail: { error: 'feature_disabled' } })).toBe(true)
    expect(isFeatureDisabled({ status: 404, detail: { error: 'not_found' } })).toBe(false)
    expect(isFeatureDisabled({ status: 403, detail: { error: 'feature_disabled' } })).toBe(false)
    expect(isFeatureDisabled(new Error('x'))).toBe(false)
    expect(isFeatureDisabled(null)).toBe(false)
  })
})
