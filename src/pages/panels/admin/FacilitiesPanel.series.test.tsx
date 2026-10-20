/**
 * Document numbers on the Facilities screen (multisite-7a): shown only with the
 * multi-site switch on, loaded only when opened; an edit sends only what
 * changed; setting up a second facility's series sends everything typed; the
 * server's refusal of a prefix is shown beside the field; a digits value out
 * of range is refused before anything is sent.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetPlatformFlags, setPlatformFlags } from '../../../shellConfig'

const api = vi.hoisted(() => ({
  listFacilities: vi.fn(),
  updateFacility: vi.fn(),
  getCreateCheck: vi.fn(),
}))
const series = vi.hoisted(() => ({
  listDocumentSeries: vi.fn(),
  saveDocumentSeries: vi.fn(),
}))

vi.mock('../../../api/facilities', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../api/facilities')>()
  return { ...actual, ...api }
})
vi.mock('../../../api/documentSeries', () => series)

import FacilitiesPanel from './FacilitiesPanel'
import type { Facility } from '../../../api/facilities'
import type { DocumentSeries } from '../../../api/documentSeries'

function facility(overrides: Partial<Facility> = {}): Facility {
  return {
    id: 1, uuid: 'u-1', code: 'MAIN', is_active: true, is_main: true,
    display_name: 'Lunaris General', site_name: 'Lunaris General',
    name: null, short_name: null, legal_name: null,
    address_line1: null, address_line2: null, city: null, district: null, state: null,
    pincode: null, gstin: null, pan: null, state_code: null, phone: null, email: null,
    hfr_id: null, abdm_hip_id: null, nabh_accreditation_no: null, nabh_valid_until: null,
    clinical_establishment_reg_no: null, timezone: null,
    address: null, supplier_name: 'Lunaris General', state_code_name: null,
    invoice_ready: false, missing_for_invoice: ['Address', 'GSTIN'],
    can_edit: true, created_at: '2026-10-09 10:00:00', created_by: null,
    updated_at: null, updated_by: null, audit_epoch_id: null,
    ...overrides,
  }
}

function row(overrides: Partial<DocumentSeries> = {}): DocumentSeries {
  return {
    id: 1, facility_id: 1, series_code: 'INV', document_type: 'invoice',
    label: 'Invoices and credit notes', prefix: 'INV-', suffix: '', pad_width: 6,
    next_number: 12, financial_year: '2026-27', resets_yearly: true, check_digit: null,
    is_configured: false, group_wide: false, next_number_preview: 'INV-2026-27-000012',
    updated_at: null, ...overrides,
  }
}

const annexe = facility({
  id: 2, uuid: 'u-2', code: 'ANX1', is_main: false, display_name: 'Annexe Clinic',
  name: 'Annexe Clinic', site_name: null,
})

beforeEach(() => {
  api.listFacilities.mockResolvedValue({
    facilities: [facility(), annexe], multi_site: true, multi_facility_data: true,
  })
  api.getCreateCheck.mockResolvedValue({ allowed: false, reasons: [] })
  series.listDocumentSeries.mockImplementation(async (id: number) => (id === 1
    ? { facility_id: 1, can_edit: true, can_add: [],
        series: [row(), row({ id: 2, series_code: 'UHID', document_type: 'uhid',
                               label: 'Hospital numbers (UHID)', prefix: '', resets_yearly: false,
                               group_wide: true, is_configured: true,
                               next_number_preview: '0000173' })] }
    : { facility_id: 2, can_edit: true, series: [],
        can_add: [{ series_code: 'RCPT', document_type: 'receipt', label: 'Receipts' }] }))
})

afterEach(() => {
  cleanup()
  resetPlatformFlags()
  vi.clearAllMocks()
})

async function openSeries(id: number) {
  const section = await screen.findByTestId(`facility-series-${id}`)
  fireEvent.click(within(section).getByRole('button', { name: 'Show document numbers' }))
  return section
}

describe('document numbers', () => {
  it('are not shown with the multi-site switch off', async () => {
    render(<FacilitiesPanel />)
    expect(await screen.findByTestId('facility-card-1')).toBeTruthy()
    expect(screen.queryByText('Show document numbers')).toBeNull()
    expect(series.listDocumentSeries).not.toHaveBeenCalled()
  })

  it('are loaded only when opened, and an edit sends only what changed', async () => {
    act(() => { setPlatformFlags({ multi_site: true, multi_facility_data: true }) })
    series.saveDocumentSeries.mockResolvedValue({
      series: row({ prefix: 'LUN/INV/', next_number_preview: 'LUN/INV/2026-27-000012' }),
      created: false, changed: ['prefix'],
    })
    render(<FacilitiesPanel />)
    await screen.findByTestId('facility-series-1')
    expect(series.listDocumentSeries).not.toHaveBeenCalled()
    const section = await openSeries(1)
    expect(await within(section).findByText('INV-2026-27-000012')).toBeTruthy()
    expect(within(section).getByText('Hospital numbers (UHID) (whole group)')).toBeTruthy()
    expect(within(section).getByText('Placeholder, not yet set by the hospital')).toBeTruthy()
    fireEvent.click(within(section).getByRole('button', { name: 'Edit invoices and credit notes' }))
    fireEvent.change(within(section).getByLabelText('Prefix'), { target: { value: 'LUN/INV/' } })
    fireEvent.click(within(section).getByRole('button', { name: 'Save numbering' }))
    await waitFor(() => expect(series.saveDocumentSeries).toHaveBeenCalledTimes(1))
    expect(series.saveDocumentSeries).toHaveBeenCalledWith(1, 'INV', { prefix: 'LUN/INV/' })
    expect(await within(section).findByRole('status')).toHaveTextContent(
      'Saved. The next number is LUN/INV/2026-27-000012.')
    await waitFor(() => expect(series.listDocumentSeries).toHaveBeenCalledTimes(2))
    // No long dash anywhere on the screen (the house writing rule).
    expect(document.body.textContent).not.toContain(String.fromCharCode(0x2014))
  })

  it("shows the server's refusal of a prefix beside the field", async () => {
    act(() => { setPlatformFlags({ multi_site: true, multi_facility_data: true }) })
    series.saveDocumentSeries.mockRejectedValue(Object.assign(new Error('That prefix could repeat a number.'), {
      status: 400,
      detail: { error: 'invalid_series', message: 'That prefix could repeat a number.',
                errors: [{ field: 'prefix', code: 'prefix_clash',
                           message: 'Facility ANX1 numbers its invoices and credit notes with INV-A.' }] },
    }))
    render(<FacilitiesPanel />)
    const section = await openSeries(1)
    fireEvent.click(await within(section).findByRole('button', { name: 'Edit invoices and credit notes' }))
    const prefix = within(section).getByLabelText('Prefix')
    fireEvent.change(prefix, { target: { value: 'INV' } })
    fireEvent.click(within(section).getByRole('button', { name: 'Save numbering' }))
    await waitFor(() => expect(prefix).toHaveAttribute('aria-invalid', 'true'))
    const described = (prefix.getAttribute('aria-describedby') || '').split(' ')
      .map(id => document.getElementById(id)?.textContent).join(' ')
    expect(described).toContain('Facility ANX1 numbers its invoices')
    expect(within(section).getByRole('alert')).toHaveTextContent('Some details need correcting')
  })

  it("sets up a second facility's series with everything typed", async () => {
    act(() => { setPlatformFlags({ multi_site: true, multi_facility_data: true }) })
    series.saveDocumentSeries.mockResolvedValue({
      series: row({ id: 9, facility_id: 2, series_code: 'RCPT', label: 'Receipts',
                    prefix: 'ANX/R-', next_number_preview: 'ANX/R-2026-27-000001' }),
      created: true, changed: ['is_configured', 'pad_width', 'prefix', 'suffix'],
    })
    render(<FacilitiesPanel />)
    const section = await openSeries(2)
    expect(await within(section).findByText('No document numbers are set up at this facility yet.'))
      .toBeTruthy()
    fireEvent.click(within(section).getByRole('button', { name: 'Set up receipts' }))
    fireEvent.change(within(section).getByLabelText('Prefix'), { target: { value: 'ANX/R-' } })
    fireEvent.click(within(section).getByRole('checkbox'))
    fireEvent.click(within(section).getByRole('button', { name: 'Set up numbering' }))
    await waitFor(() => expect(series.saveDocumentSeries).toHaveBeenCalledWith(
      2, 'RCPT', { prefix: 'ANX/R-', suffix: '', pad_width: 6, is_configured: true }))
    expect(await within(section).findByRole('status')).toHaveTextContent(
      'Receipts are now numbered here. The next number is ANX/R-2026-27-000001.')
  })

  it('refuses digits out of range before sending anything', async () => {
    act(() => { setPlatformFlags({ multi_site: true, multi_facility_data: true }) })
    render(<FacilitiesPanel />)
    const section = await openSeries(1)
    fireEvent.click(await within(section).findByRole('button', { name: 'Edit invoices and credit notes' }))
    const digits = within(section).getByLabelText('Digits')
    fireEvent.change(digits, { target: { value: '30' } })
    fireEvent.click(within(section).getByRole('button', { name: 'Save numbering' }))
    await waitFor(() => expect(digits).toHaveAttribute('aria-invalid', 'true'))
    expect(series.saveDocumentSeries).not.toHaveBeenCalled()
  })

  it('hides itself when the server says the switch is off', async () => {
    act(() => { setPlatformFlags({ multi_site: true, multi_facility_data: false }) })
    series.listDocumentSeries.mockRejectedValue(Object.assign(new Error('off'), {
      status: 404, detail: { error: 'feature_disabled', feature: 'multi_site' },
    }))
    render(<FacilitiesPanel />)
    await openSeries(1)
    await waitFor(() => expect(screen.queryByTestId('facility-series-1')).toBeNull())
  })
})
