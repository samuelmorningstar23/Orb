/**
 * The shared Laboratory API base (lis-1). Every area file (master, and the
 * later orders, specimens, results and criticals) builds on these helpers, so
 * the error mapping, the query string and the path rule are pinned once, here.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../client', async () => {
  const actual = await vi.importActual<typeof import('../client')>('../client')
  return { ...actual, apiFetch: vi.fn() }
})

const client = await import('../client')
const apiFetch = vi.mocked(client.apiFetch)

import {
  LAB_API_PREFIX,
  LAB_MODULE_ID,
  LabApiError,
  getLabMeta,
  labGet,
  labPost,
  labQuery,
  toLabError,
} from './index'
import { approveLabReferenceRange, getLabTest, searchLabCatalogue } from './master'

beforeEach(() => {
  apiFetch.mockReset()
})

describe('laboratory API base', () => {
  it('uses the module id and prefix the server publishes', () => {
    expect(LAB_MODULE_ID).toBe('lab')
    expect(LAB_API_PREFIX).toBe('/lab')
  })

  it('probes /lab/meta through apiFetch', async () => {
    apiFetch.mockResolvedValue({ module: 'lab', state: 'draining' })
    await expect(getLabMeta()).resolves.toEqual({ module: 'lab', state: 'draining' })
    expect(apiFetch).toHaveBeenCalledWith('/lab/meta', {})
  })

  it('builds a query string without empty, null or undefined values', () => {
    expect(labQuery({ q: 'potassium', limit: 5, all: false, x: '', y: null, z: undefined }))
      .toBe('?q=potassium&limit=5&all=false')
    expect(labQuery()).toBe('')
  })

  it('refuses a path that does not start with a slash', async () => {
    await expect(labGet('meta')).rejects.toThrow(/start with "\/"/)
    expect(apiFetch).not.toHaveBeenCalled()
  })

  it('keeps the server code on a refusal', async () => {
    apiFetch.mockRejectedValue(Object.assign(new Error('This overlaps a current range.'), {
      status: 409, detail: { error: 'overlap', message: 'This overlaps a current range.' },
    }))
    const err = (await labPost('/master/ranges', {}).catch((e: unknown) => e)) as LabApiError
    expect(err).toBeInstanceOf(LabApiError)
    expect(err).toMatchObject({ status: 409, code: 'overlap' })
    expect(err.isConflict).toBe(true)
    expect(err.isModuleDisabled).toBe(false)
  })

  it('reads module_disabled, not found, forbidden and no reply apart', () => {
    const off = toLabError({ status: 404, detail: { error: 'module_disabled', module: 'lab' } })
    expect(off.isModuleDisabled).toBe(true)
    expect(off.isNotFound).toBe(false)
    expect(toLabError({ status: 404, detail: { error: 'not_found' } }).isNotFound).toBe(true)
    const denied = toLabError({ status: 403, permissions: { error: 'permission_denied' } })
    expect(denied.isForbidden).toBe(true)
    expect(denied.code).toBe('permission_denied')
    const gone = toLabError(new TypeError('Failed to fetch'))
    expect(gone.isUnreachable).toBe(true)
    expect(gone.code).toBe('network_error')
    expect(gone.message).toBe('Could not reach the laboratory. Check the connection and try again.')
  })
})

describe('laboratory master client', () => {
  it('sends typed values as strings and encodes test ids in the path', async () => {
    apiFetch.mockResolvedValue({})
    await approveLabReferenceRange({ test_id: 'LOINC:2823-3', low: '3.50', high: '5.1',
      method_note: 'Analyser A' })
    expect(apiFetch).toHaveBeenLastCalledWith('/lab/master/ranges', {
      method: 'POST',
      body: JSON.stringify({ test_id: 'LOINC:2823-3', low: '3.50', high: '5.1',
        method_note: 'Analyser A' }),
    })
    await getLabTest('LOINC:2823-3')
    expect(apiFetch).toHaveBeenLastCalledWith('/lab/master/tests/LOINC%3A2823-3', {})
    await searchLabCatalogue('cbc', 5)
    expect(apiFetch).toHaveBeenLastCalledWith('/lab/catalogue?q=cbc&limit=5', {})
  })
})
