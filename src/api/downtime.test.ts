/**
 * fetchWardPackHtml is the one downtime call that cannot go through apiFetch:
 * the ward pack is a text/html document, and apiFetch would json() it. It lived
 * inside the page component for that reason; it belongs with the other downtime
 * calls, and its contract is pinned here so the move cannot quietly change it.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./client', () => ({
  API_BASE: '/api',
  apiFetch: vi.fn(),
  buildHeaders: () => ({ 'X-CSRF-Token': 'csrf-1' }),
  handleResponseError: async (response: { status: number }) => {
    throw new Error(`API error: ${response.status}`)
  },
}))

import { fetchWardPackHtml } from './downtime'

beforeEach(() => {
  vi.unstubAllGlobals()
})

describe('fetchWardPackHtml', () => {
  it('fetches the ward pack as raw text with session credentials and auth headers', async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, text: async () => '<!DOCTYPE html>ward pack' }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchWardPackHtml('P-TEST-1')).resolves.toBe('<!DOCTYPE html>ward pack')
    expect(fetchMock).toHaveBeenCalledWith('/api/downtime/P-TEST-1/ward-pack', {
      credentials: 'include',
      headers: { 'X-CSRF-Token': 'csrf-1' },
    })
  })

  it('raises through the shared error path so a scope refusal keeps its message', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 404, text: async () => '' })))
    await expect(fetchWardPackHtml('P-NOPE')).rejects.toThrow(/404/)
  })
})
