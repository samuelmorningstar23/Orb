import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { workbenchDownload } from './localWorkbenchDownload'
import { ApiIdentityChangedError, handleResponseError } from './client'
let owner: string | null = 'SYNTHETIC-A'
vi.mock('./client', () => ({
  API_BASE: 'http://localhost/api',
  ApiIdentityChangedError: class extends Error {},
  buildHeaders: vi.fn(method => ({ 'X-Test-Method': method, 'X-CSRF-Token': 'synthetic-csrf' })),
  getUser: () => owner ? { id: owner } : null,
  handleResponseError: vi.fn(async () => { throw new Error('Synthetic HTTP refusal') }),
}))
beforeEach(() => { owner = 'SYNTHETIC-A'; vi.clearAllMocks(); vi.stubGlobal('fetch', vi.fn()); vi.stubGlobal('URL', class extends URL { static createObjectURL = vi.fn(() => 'blob:synthetic'); static revokeObjectURL = vi.fn() }); vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {}) })
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })
describe('session-bound CSV download', () => {
  it('uses the current cookie/CSRF headers and releases its object URL', async () => {
    vi.mocked(fetch).mockResolvedValue(new Response('synthetic,csv'))
    await workbenchDownload('/reporting/query.csv', 'synthetic.csv', { dataset: 'patients' })
    expect(fetch).toHaveBeenCalledWith('http://localhost/api/reporting/query.csv', expect.objectContaining({ method: 'POST', credentials: 'include', headers: { 'X-Test-Method': 'POST', 'X-CSRF-Token': 'synthetic-csrf' }, body: '{"dataset":"patients"}' }))
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledOnce()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:synthetic')
  })
  it('does not save a response that completes after an account switch', async () => {
    vi.mocked(fetch).mockImplementation(async () => { owner = 'SYNTHETIC-B'; return new Response('private,csv') })
    await expect(workbenchDownload('/finance-tools/test.csv', 'test.csv')).rejects.toBeInstanceOf(ApiIdentityChangedError)
    expect(URL.createObjectURL).not.toHaveBeenCalled()
  })
  it('checks identity again after reading the response body', async () => {
    vi.mocked(fetch).mockResolvedValue({ ok: true, blob: async () => { owner = null; return new Blob(['private,csv']) } } as Response)
    await expect(workbenchDownload('/finance-tools/test.csv', 'test.csv')).rejects.toBeInstanceOf(ApiIdentityChangedError)
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled()
  })
  it('never downloads an HTTP failure as a successful CSV', async () => {
    vi.mocked(fetch).mockResolvedValue(new Response('refused', { status: 403 }))
    await expect(workbenchDownload('/finance-tools/test.csv', 'test.csv')).rejects.toThrow('Synthetic HTTP refusal')
    expect(handleResponseError).toHaveBeenCalledOnce()
    expect(URL.createObjectURL).not.toHaveBeenCalled()
  })
})
