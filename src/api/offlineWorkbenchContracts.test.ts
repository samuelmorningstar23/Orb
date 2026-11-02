import { beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch } from './client'
import { getReportDefinitions } from './reporting'
import { getPayoutStatements } from './financeTools'

vi.mock('./client', () => ({ apiFetch: vi.fn() }))
vi.mock('./localWorkbenchDownload', () => ({ workbenchDownload: vi.fn() }))
beforeEach(() => vi.resetAllMocks())

describe('offline workbench collection response contracts', () => {
  it('unwraps the reporting route definitions envelope into the panel list', async () => {
    const definitions = [{ id: 'synthetic-definition', name: 'Training census', version: 1, query: { dataset: 'patients' } }]
    vi.mocked(apiFetch).mockResolvedValue({ definitions })
    expect(await getReportDefinitions()).toBe(definitions)
    expect(apiFetch).toHaveBeenCalledWith('/reporting/definitions')
  })
  it('unwraps the finance route statements envelope and keeps the requested facility', async () => {
    const statements = [{ id: 'synthetic-statement', status: 'draft', total_paise: 10000 }]
    vi.mocked(apiFetch).mockResolvedValue({ statements })
    expect(await getPayoutStatements(7)).toBe(statements)
    expect(apiFetch).toHaveBeenCalledWith('/finance-tools/payout-statements?facility_id=7')
  })
  it('returns valid empty collections from both actual route envelopes', async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce({ definitions: [] }).mockResolvedValueOnce({ statements: [] })
    expect(await getReportDefinitions()).toEqual([])
    expect(await getPayoutStatements()).toEqual([])
  })
  it('preserves an authoritative refusal rather than returning an empty collection', async () => {
    const refusal = Object.assign(new Error('Access refused'), { status: 403 })
    vi.mocked(apiFetch).mockRejectedValue(refusal)
    await expect(getReportDefinitions()).rejects.toBe(refusal)
    await expect(getPayoutStatements()).rejects.toBe(refusal)
  })
})
