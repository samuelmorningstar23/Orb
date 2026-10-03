/**
 * The shared Pharmacy store API base. Every area file (masters, purchasing,
 * receipts and the rest) builds on these helpers, so the error mapping, the
 * query string and the path rule are pinned once, here.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../client', async () => {
  const actual = await vi.importActual<typeof import('../client')>('../client')
  return { ...actual, apiFetch: vi.fn() }
})

const client = await import('../client')
const apiFetch = vi.mocked(client.apiFetch)

import {
  PHARMACY_STORE_API_PREFIX,
  PHARMACY_STORE_MODULE_ID,
  PharmacyStoreApiError,
  getPharmacyStoreMeta,
  pharmacyStoreDownload,
  pharmacyStoreGet,
  pharmacyStorePost,
  pharmacyStoreQuery,
  toPharmacyStoreError,
} from './index'

beforeEach(() => {
  apiFetch.mockReset()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('pharmacy store API base', () => {
  it('uses the module id and prefix the server publishes', () => {
    expect(PHARMACY_STORE_MODULE_ID).toBe('pharmacy-store')
    expect(PHARMACY_STORE_API_PREFIX).toBe('/pharmacy-store')
  })

  it('probes /pharmacy-store/meta through apiFetch', async () => {
    apiFetch.mockResolvedValue({ module: 'pharmacy-store' })
    await expect(getPharmacyStoreMeta()).resolves.toEqual({ module: 'pharmacy-store' })
    expect(apiFetch).toHaveBeenCalledWith('/pharmacy-store/meta', {})
  })

  it('builds a query string without empty, null or undefined values', () => {
    expect(pharmacyStoreQuery({ store: 'MAIN', page: 2, all: false, q: '', x: null, y: undefined }))
      .toBe('?store=MAIN&page=2&all=false')
    expect(pharmacyStoreQuery({})).toBe('')
  })

  it('sends GET parameters and POST bodies as the server expects', async () => {
    apiFetch.mockResolvedValue({ ok: true })
    await pharmacyStoreGet('/stock', { store: 'MAIN', page: undefined })
    expect(apiFetch).toHaveBeenLastCalledWith('/pharmacy-store/stock?store=MAIN', {})
    await pharmacyStorePost('/purchase-orders', { vendor: 'V1' })
    expect(apiFetch).toHaveBeenLastCalledWith('/pharmacy-store/purchase-orders',
      { method: 'POST', body: JSON.stringify({ vendor: 'V1' }) })
  })

  it('refuses a path that does not start with a slash', async () => {
    await expect(pharmacyStoreGet('stock')).rejects.toThrow(/start with "\/"/)
    expect(apiFetch).not.toHaveBeenCalled()
  })

  it('keeps the server code on a refusal', async () => {
    apiFetch.mockRejectedValue(Object.assign(new Error('Already received.'), {
      status: 409, detail: { error: 'already_received', message: 'Already received.' },
    }))
    const err = (await pharmacyStorePost('/purchase-orders/4/cancel')
      .catch((e: unknown) => e)) as PharmacyStoreApiError
    expect(err).toBeInstanceOf(PharmacyStoreApiError)
    expect(err).toMatchObject({ status: 409, code: 'already_received', message: 'Already received.' })
    expect(err.isConflict).toBe(true)
    expect(err.isModuleDisabled).toBe(false)
  })

  it('reads module_disabled, not found and forbidden apart', () => {
    const off = toPharmacyStoreError(Object.assign(new Error('off'), {
      status: 404, detail: { error: 'module_disabled' } }))
    expect(off.isModuleDisabled).toBe(true)
    expect(off.isNotFound).toBe(false)

    const missing = toPharmacyStoreError(Object.assign(new Error('No such order.'), {
      status: 404, detail: { error: 'not_found' } }))
    expect(missing.isNotFound).toBe(true)
    expect(missing.isModuleDisabled).toBe(false)

    // apiFetch puts a 403 body on `permissions`, not `detail`.
    const denied = toPharmacyStoreError(Object.assign(new Error('Approver must differ.'), {
      status: 403, permissions: { error: 'same_person', message: 'Approver must differ.' } }))
    expect(denied).toMatchObject({ status: 403, code: 'same_person' })
    expect(denied.isForbidden).toBe(true)
  })

  it('reports no reply in plain words', () => {
    const err = toPharmacyStoreError(new TypeError('Failed to fetch'))
    expect(err).toMatchObject({ status: 0, code: 'network_error' })
    expect(err.isUnreachable).toBe(true)
    expect(err.message).toBe('Could not reach the pharmacy store. Check the connection and try again.')
  })

  it('falls back to http_<status> when the server sent no code', () => {
    const err = toPharmacyStoreError(Object.assign(new Error('Server error'), { status: 500 }))
    expect(err.code).toBe('http_500')
  })

  it('downloads a file, and maps a refused download to the same error', async () => {
    const ok = new Response('a,b\n1,2\n', { status: 200 })
    const fetchMock = vi.fn().mockResolvedValueOnce(ok).mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: { error: 'module_disabled', message: 'off' } }),
        { status: 404, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)

    const blob = await pharmacyStoreDownload('/ndps/export.csv', { store: 'VAULT' })
    expect(await blob.text()).toBe('a,b\n1,2\n')
    expect(String(fetchMock.mock.calls[0][0])).toMatch(/\/api\/pharmacy-store\/ndps\/export\.csv\?store=VAULT$/)

    const err = (await pharmacyStoreDownload('/ndps/export.csv')
      .catch((e: unknown) => e)) as PharmacyStoreApiError
    expect(err).toBeInstanceOf(PharmacyStoreApiError)
    expect(err.isModuleDisabled).toBe(true)
  })
})
