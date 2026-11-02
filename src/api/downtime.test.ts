/**
 * The HTML ward pack uses the same session transport as JSON while preserving
 * its raw document bytes and the server's refusal.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { fetchWardPackHtml } from './downtime'
import { API_BASE, buildHeaders } from './client'

beforeEach(() => {
  vi.unstubAllGlobals()
})

describe('fetchWardPackHtml', () => {
  it('fetches the ward pack as raw text with session credentials and auth headers', async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, text: async () => '<!DOCTYPE html>ward pack' }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchWardPackHtml('P-TEST-1')).resolves.toBe('<!DOCTYPE html>ward pack')
    expect(fetchMock).toHaveBeenCalledWith(`${API_BASE}/downtime/P-TEST-1/ward-pack`, {
      credentials: 'include',
      headers: buildHeaders('GET'),
    })
  })

  it('raises through the shared error path so a scope refusal keeps its message', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'This patient is outside your ward.' }), { status: 404 })))
    await expect(fetchWardPackHtml('P-NOPE')).rejects.toMatchObject({ message: 'This patient is outside your ward.', status: 404 })
  })
})
