/**
 * BillingPanel shows where each price list applies (multisite-13): on a
 * multi-site install the price list tab lists the price lists with "Applies
 * to" (the whole group or a facility) and which is a default, and the rate
 * form names it beside each list. With the multi-site switch off and one
 * facility on record the tab is exactly what it was.
 *
 * The API client is mocked at `apiFetch`, as in BillingPanel.test.tsx.
 */
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { resetPlatformFlags, setPlatformFlags } from '../../shellConfig'

const ITEM = {
  id: 1, code: 'CONS-GEN', name: 'General consultation', category: 'consultation',
  department: null, uom: 'each', gst_treatment: 'exempt', gst_rate_bp: 0,
  hsn_sac: null, default_rate_paise: 50000, rate_is_configured: 1, is_active: 1,
  notes: null,
}

const PLANS = [
  { id: 1, code: 'SELFPAY', name: 'Self-pay (cash)', plan_type: 'self_pay',
    is_default: 1, is_active: 1, facility_id: null, applies_to: 'Whole group' },
  { id: 11, code: 'ANX-RATES', name: 'Annexe rates', plan_type: 'self_pay',
    is_default: 1, is_active: 1, facility_id: 2, applies_to: 'Annexe Clinic' },
  { id: 5, code: 'CORP26', name: 'Corporate 2026', plan_type: 'corporate',
    is_default: 0, is_active: 1, facility_id: null, applies_to: 'Whole group' },
]

async function fakeApi(path: string) {
  if (path.startsWith('/billing/config')) {
    return {
      module: 'billing', enabled: true, discharge_financial_enforce: false,
      default_tariff_plan: PLANS[0], service_item_count: 1,
      priced_service_item_count: 1, unpriced_service_item_count: 0,
      financial_year: '2026-27',
    }
  }
  if (path.startsWith('/billing/service-items')) return { items: [ITEM] }
  if (path.startsWith('/billing/tariff-plans')) return { plans: PLANS }
  if (path.startsWith('/billing/invoices')) return { invoices: [] }
  throw new Error(`unmocked path ${path}`)
}

vi.mock('../../api/client', () => ({
  apiFetch: (path: string) => fakeApi(path),
}))

import BillingPanel from './BillingPanel'

afterEach(() => {
  cleanup()
  resetPlatformFlags()
  vi.clearAllMocks()
})

async function openPriceListTab() {
  fireEvent.click(await screen.findByRole('button', { name: 'Price list' }))
  await screen.findByText('General consultation')
}

describe('where each price list applies', () => {
  it('is not shown with the multi-site switch off and one facility', async () => {
    render(<BillingPanel />)
    await openPriceListTab()
    expect(screen.queryByRole('heading', { name: 'Price lists' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Set rate' }))
    const select = await screen.findByLabelText('Tariff plan') as HTMLSelectElement
    expect(Array.from(select.options).map(o => o.textContent)).toEqual([
      'Self-pay (cash) (SELFPAY)', 'Annexe rates (ANX-RATES)', 'Corporate 2026 (CORP26)',
    ])
  })

  it('lists each price list with what it applies to on a multi-site install', async () => {
    act(() => { setPlatformFlags({ multi_site: true, multi_facility_data: false }) })
    render(<BillingPanel />)
    await openPriceListTab()
    const table = (await screen.findByRole('heading', { name: 'Price lists' })).closest('section')!
    const rows = within(table).getAllByRole('row')
    expect(rows[0]).toHaveTextContent('Applies to')
    expect(rows[1]).toHaveTextContent('Self-pay (cash) (SELFPAY)')
    expect(rows[1]).toHaveTextContent('Whole group')
    expect(rows[1]).toHaveTextContent("The group's default")
    expect(rows[2]).toHaveTextContent('Annexe Clinic')
    expect(rows[2]).toHaveTextContent('Default at Annexe Clinic')
    expect(rows[3]).not.toHaveTextContent('Default')
  })

  it('names where each list applies in the rate form', async () => {
    act(() => { setPlatformFlags({ multi_site: false, multi_facility_data: true }) })
    render(<BillingPanel />)
    await openPriceListTab()
    fireEvent.click(screen.getByRole('button', { name: 'Set rate' }))
    const select = await screen.findByLabelText('Tariff plan') as HTMLSelectElement
    expect(Array.from(select.options).map(o => o.textContent)).toEqual([
      'Self-pay (cash) (SELFPAY), Whole group',
      'Annexe rates (ANX-RATES), Annexe Clinic',
      'Corporate 2026 (CORP26), Whole group',
    ])
  })
})
