/**
 * "Post bed days through [date]" on the Charges tab (W136, adt-15). With beds
 * and admissions off a bed charge item must be chosen, as the server requires;
 * with it on each day can be priced by the class of the bed at midnight, and
 * no service code is sent. A day that could not be priced is named with the
 * reason, and leave days say whether they were charged.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { resetModuleFlags, setModuleFlags } from '../../shellConfig'

const calls: { path: string; method: string; body: unknown }[] = []
let bedItems: unknown[] = []
let bedDaysAnswer: unknown = null

const SUMMARY = {
  patient_id: 'P1', admission_id: 'ADM-1', live_invoice_count: 0, draft_invoice_count: 0,
  outstanding_paise: 0, outstanding_display: '₹0.00',
  unbilled_charge_count: 0, unbilled_total_paise: 0, unbilled_total_display: '₹0.00',
  unpriced_charge_count: 0,
  deposit_balance_paise: 0, deposit_balance_display: '₹0.00', open_claim_count: 0,
  net_due_paise: 0, net_due_display: '₹0.00',
}

async function fakeApi(path: string, options: RequestInit = {}) {
  calls.push({
    path,
    method: (options.method as string) || 'GET',
    body: options.body ? JSON.parse(options.body as string) : null,
  })
  if (path.startsWith('/billing/config')) {
    return {
      module: 'billing', enabled: true, discharge_financial_enforce: false,
      default_tariff_plan: null, service_item_count: 0, priced_service_item_count: 0,
      unpriced_service_item_count: 0, financial_year: '2026-27',
    }
  }
  if (path.startsWith('/billing/service-items')) return { items: bedItems }
  if (path.startsWith('/billing/tariff-plans')) return { plans: [] }
  if (path.startsWith('/billing/invoices')) return { invoices: [] }
  if (path.includes('/bed-days')) return bedDaysAnswer
  if (path.startsWith('/vigil/patients')) {
    return [{ id: 'P1', name: 'Saoirse Venkat', mrn: 'MRN-1', ward: 'W1', bed: '3',
              admission_id: 'ADM-1' }]
  }
  if (path.startsWith('/vigil/discharged')) return []
  if (path.includes('/discharge-advisory')) {
    return { ...SUMMARY, advisory_status: 'clear', reasons: [], enforced: false,
             blocks_discharge: false, headline: 'Nothing outstanding',
             message: 'Advisory only: this does not prevent the discharge.',
             unpriceable_event_count: 0, uncaptured_event_count: 0,
             checked_at: '2026-10-02T10:00:00' }
  }
  if (path.includes('/charges')) return { patient_id: 'P1', charges: [], summary: SUMMARY }
  throw new Error(`unmocked path ${path}`)
}

vi.mock('../../api/client', () => ({
  apiFetch: (path: string, options: RequestInit = {}) => fakeApi(path, options),
}))

import BillingPanel from './BillingPanel'

const BED_ITEM = {
  id: 1, code: 'BED-GEN', name: 'General ward bed', category: 'bed', department: null,
  uom: 'day', gst_treatment: 'exempt', gst_rate_bp: 0, hsn_sac: null,
  default_rate_paise: 0, rate_is_configured: 1, is_active: 1, notes: null,
}

afterEach(() => {
  calls.length = 0
  bedItems = []
  bedDaysAnswer = null
  resetModuleFlags()
  vi.clearAllMocks()
})

async function openBedDays() {
  render(<BillingPanel />)
  fireEvent.click(await screen.findByRole('button', { name: 'Charges' }))
  const picker = await screen.findByLabelText('Patient')
  await waitFor(() => expect(within(picker as HTMLElement).getByText(/Saoirse Venkat/))
    .toBeInTheDocument())
  fireEvent.change(picker, { target: { value: 'P1' } })
  fireEvent.click(await screen.findByRole('button', { name: 'Post bed days' }))
  return screen.findByTestId('bed-days-form')
}

function sent() {
  return calls.filter(c => c.path === '/billing/patients/P1/bed-days' && c.method === 'POST')
}

describe('posting bed days', () => {
  it('with beds and admissions off, a bed charge item is chosen and sent', async () => {
    bedItems = [BED_ITEM]
    bedDaysAnswer = {
      from: '2026-10-01', through: '2026-10-03', pricing: 'service_code', posted_count: 2,
      posted_total_display: '₹5,000.00', already_present: 0,
      unpriced_days: ['2026-10-01'],
      unpriced_detail: [{ day: '2026-10-01', reason: 'rate_not_configured',
                          message: 'No price on this tariff for that day' }],
      leave_days: [], leave_rent_charged: null,
    }
    const form = await openBedDays()
    const select = within(form).getByLabelText('Price each day by') as HTMLSelectElement
    expect(within(select).queryByText(/class of the bed/)).toBeNull()
    expect(select.value).toBe('BED-GEN')
    fireEvent.click(within(form).getByRole('button', { name: 'Post bed days up to today' }))
    await waitFor(() => expect(sent()).toHaveLength(1))
    expect(sent()[0].body).toEqual({ admission_id: 'ADM-1', service_code: 'BED-GEN' })
    const result = await screen.findByTestId('bed-days-result')
    expect(result).toHaveTextContent('2 bed days posted, ₹5,000.00')
    expect(result).toHaveTextContent('1 day could not be priced and was not charged')
    expect(result).toHaveTextContent('1 Oct 2026')
    expect(result).toHaveTextContent('No price on this tariff for that day')
  })

  it('with beds and admissions off and no bed item on the price list, nothing can be sent', async () => {
    const form = await openBedDays()
    expect(form).toHaveTextContent('No bed charge item is on the price list')
    expect(within(form).getByRole('button', { name: 'Post bed days up to today' })).toBeDisabled()
  })

  it('with beds and admissions on, each day is priced by its bed class and no code is sent',
    async () => {
      setModuleFlags({ adt: true })
      bedItems = [BED_ITEM]
      bedDaysAnswer = {
        from: '2026-10-01', through: '2026-10-03', pricing: 'bed_class', posted_count: 1,
        posted_total_display: '₹9,000.00', already_present: 1, unpriced_days: ['2026-10-03'],
        unpriced_detail: [{ day: '2026-10-03', reason: 'no_bed_class',
                            message: 'The bed has no bed class in bed setup', bed: '12' }],
        leave_days: ['2026-10-02'], leave_rent_charged: false,
      }
      const form = await openBedDays()
      const select = within(form).getByLabelText('Price each day by') as HTMLSelectElement
      expect(select.value).toBe('')
      fireEvent.change(within(form).getByLabelText('Through'), { target: { value: '2026-10-03' } })
      fireEvent.click(within(form).getByRole('button', { name: 'Post bed days through 3 Oct 2026' }))
      await waitFor(() => expect(sent()).toHaveLength(1))
      expect(sent()[0].body).toEqual({ admission_id: 'ADM-1', through: '2026-10-03' })
      const result = await screen.findByTestId('bed-days-result')
      expect(result).toHaveTextContent('1 day was already on the bill')
      expect(result).toHaveTextContent('The bed has no bed class in bed setup, bed 12')
      expect(result).toHaveTextContent('On leave at midnight: 2 Oct 2026')
      expect(result).toHaveTextContent('does not charge room rent for leave days')
    })
})
