/**
 * Price lists on the Facilities screen (multisite-13): shown only with the
 * multi-site switch on and loaded only when opened; the screen says which
 * list a facility's patients are priced on and where each list applies; the
 * default price list editor sends the chosen list (null for the group's
 * default); a group list other than the default is offered only to an
 * account with group-wide access; a refusal is shown in the server's words;
 * a 404 on load (switch off, billing not run here) hides the section.
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
const prices = vi.hoisted(() => ({
  getFacilityPriceLists: vi.fn(),
  setFacilityDefaultPriceList: vi.fn(),
  setPriceListAppliesTo: vi.fn(),
}))

vi.mock('../../../api/facilities', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../api/facilities')>()
  return { ...actual, ...api }
})
vi.mock('../../../api/documentSeries', () => series)
vi.mock('../../../api/priceLists', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../../api/priceLists')>()
  return { ...actual, ...prices }
})

import FacilitiesPanel from './FacilitiesPanel'
import type { Facility } from '../../../api/facilities'
import type { FacilityPriceLists, PriceList } from '../../../api/priceLists'

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

const annexe = facility({
  id: 2, uuid: 'u-2', code: 'ANX1', is_main: false, display_name: 'Annexe Clinic',
  name: 'Annexe Clinic', site_name: null,
})

function plan(overrides: Partial<PriceList> = {}): PriceList {
  return {
    id: 1, code: 'SELFPAY', name: 'Self-pay (cash)', plan_type: 'self_pay',
    is_default: true, is_active: true, facility_id: null, applies_to: 'Whole group',
    used_by_payers: 0, ...overrides,
  }
}

const GROUP_DEFAULT = plan()
const SPARE = plan({ id: 5, code: 'CORP26', name: 'Corporate 2026', is_default: false })
const PAYER_LIST = plan({
  id: 6, code: 'TPA-VAR', name: 'Vardane schedule', is_default: false, used_by_payers: 1,
})
const OWN = plan({
  id: 11, code: 'ANX-RATES', name: 'Annexe rates', is_default: false, facility_id: 2,
  applies_to: 'Annexe Clinic',
})

function lists(overrides: Partial<FacilityPriceLists> = {}): FacilityPriceLists {
  return {
    facility: { id: 2, code: 'ANX1', name: 'Annexe Clinic', is_active: true },
    default_plan: GROUP_DEFAULT, default_source: 'group',
    plans: [OWN, GROUP_DEFAULT, SPARE, PAYER_LIST],
    can_edit: true, group_wide: true, ...overrides,
  }
}

beforeEach(() => {
  api.listFacilities.mockResolvedValue({
    facilities: [facility(), annexe], multi_site: true, multi_facility_data: true,
  })
  api.getCreateCheck.mockResolvedValue({ allowed: false, reasons: [] })
  series.listDocumentSeries.mockResolvedValue({ facility_id: 1, series: [], can_add: [], can_edit: true })
  prices.getFacilityPriceLists.mockResolvedValue(lists())
})

afterEach(() => {
  cleanup()
  resetPlatformFlags()
  vi.clearAllMocks()
})

async function openPrices(id: number) {
  const section = await screen.findByTestId(`facility-price-lists-${id}`)
  fireEvent.click(within(section).getByRole('button', { name: 'Show price lists' }))
  return section
}

function switchOn() {
  act(() => { setPlatformFlags({ multi_site: true, multi_facility_data: true }) })
}

describe('price lists', () => {
  it('are not shown with the multi-site switch off', async () => {
    render(<FacilitiesPanel />)
    expect(await screen.findByTestId('facility-card-1')).toBeTruthy()
    expect(screen.queryByText('Show price lists')).toBeNull()
    expect(prices.getFacilityPriceLists).not.toHaveBeenCalled()
  })

  it('are loaded only when opened, and say what prices patients and where each list applies', async () => {
    switchOn()
    render(<FacilitiesPanel />)
    await screen.findByTestId('facility-price-lists-2')
    expect(prices.getFacilityPriceLists).not.toHaveBeenCalled()
    const section = await openPrices(2)
    expect(prices.getFacilityPriceLists).toHaveBeenCalledWith(2)
    expect(await within(section).findByTestId('facility-price-default-2')).toHaveTextContent(
      "Patients billed here are priced on Self-pay (cash) (SELFPAY), the group's default.")
    const rows = within(section).getAllByRole('row')
    expect(rows[1]).toHaveTextContent('Annexe rates (ANX-RATES)')
    expect(rows[1]).toHaveTextContent('Annexe Clinic')
    expect(rows[2]).toHaveTextContent('Whole group')
    expect(rows[2]).toHaveTextContent('Prices patients here')
    expect(rows[3]).toHaveTextContent('Corporate 2026 (CORP26)')
    expect(rows[3]).not.toHaveTextContent('default')
  })

  it('names the group default apart from a facility’s own default', async () => {
    switchOn()
    prices.getFacilityPriceLists.mockResolvedValue(lists({
      default_plan: { ...OWN, is_default: true }, default_source: 'facility',
      plans: [{ ...OWN, is_default: true }, GROUP_DEFAULT],
    }))
    render(<FacilitiesPanel />)
    const section = await openPrices(2)
    expect(await within(section).findByTestId('facility-price-default-2')).toHaveTextContent(
      "Patients billed here are priced on Annexe rates (ANX-RATES), this facility's own default.")
    const rows = within(section).getAllByRole('row')
    expect(rows[1]).toHaveTextContent('Prices patients here')
    expect(rows[2]).toHaveTextContent("The group's default")
  })

  it('offers the group default, the facility’s own lists and spare group lists, and sends the choice', async () => {
    switchOn()
    prices.setFacilityDefaultPriceList.mockResolvedValue({
      ...lists({ default_plan: { ...OWN, is_default: true }, default_source: 'facility' }),
      changed: true, moved: false,
    })
    render(<FacilitiesPanel />)
    const section = await openPrices(2)
    const select = await within(section).findByLabelText('Default price list') as HTMLSelectElement
    const options = Array.from(select.options).map(o => o.textContent)
    expect(options).toEqual([
      "The group's default, Self-pay (cash) (SELFPAY)",
      'Annexe rates (ANX-RATES)',
      'Corporate 2026 (CORP26), to be kept for this facility',
    ])
    expect(options.join(' ')).not.toContain('Vardane')
    fireEvent.change(select, { target: { value: '11' } })
    fireEvent.click(within(section).getByRole('button', { name: 'Save default price list' }))
    await waitFor(() => expect(prices.setFacilityDefaultPriceList).toHaveBeenCalledWith(2, 11))
    expect(await within(section).findByRole('status')).toHaveTextContent(
      'Saved. Bills raised here from now on are priced on Annexe rates (ANX-RATES).')
  })

  it('sends null for the group’s default and names a list kept for the facility', async () => {
    switchOn()
    prices.getFacilityPriceLists.mockResolvedValue(lists({
      default_plan: { ...OWN, is_default: true }, default_source: 'facility',
      plans: [{ ...OWN, is_default: true }, GROUP_DEFAULT, SPARE],
    }))
    prices.setFacilityDefaultPriceList.mockResolvedValue({ ...lists(), changed: true, moved: false })
    render(<FacilitiesPanel />)
    const section = await openPrices(2)
    const select = await within(section).findByLabelText('Default price list') as HTMLSelectElement
    expect(select.value).toBe('11')
    fireEvent.change(select, { target: { value: '' } })
    fireEvent.click(within(section).getByRole('button', { name: 'Save default price list' }))
    await waitFor(() => expect(prices.setFacilityDefaultPriceList).toHaveBeenCalledWith(2, null))
  })

  it('says when a group list was kept for the facility', async () => {
    switchOn()
    prices.setFacilityDefaultPriceList.mockResolvedValue({
      ...lists({
        default_plan: { ...SPARE, facility_id: 2, is_default: true, applies_to: 'Annexe Clinic' },
        default_source: 'facility',
      }),
      changed: true, moved: true,
    })
    render(<FacilitiesPanel />)
    const section = await openPrices(2)
    fireEvent.change(await within(section).findByLabelText('Default price list'), { target: { value: '5' } })
    fireEvent.click(within(section).getByRole('button', { name: 'Save default price list' }))
    expect(await within(section).findByRole('status')).toHaveTextContent(
      'Saved. Corporate 2026 (CORP26) now applies to this facility only and is its default.')
  })

  it('shows a refusal in the server’s words', async () => {
    switchOn()
    const refused = Object.assign(new Error('Only an account with group-wide access may change a price list that applies to the whole group.'), {
      status: 403,
      detail: { error: 'group_wide_access_required', message: 'Only an account with group-wide access may change a price list that applies to the whole group.' },
    })
    prices.setFacilityDefaultPriceList.mockRejectedValue(refused)
    render(<FacilitiesPanel />)
    const section = await openPrices(2)
    fireEvent.change(await within(section).findByLabelText('Default price list'), { target: { value: '5' } })
    fireEvent.click(within(section).getByRole('button', { name: 'Save default price list' }))
    expect(await within(section).findByRole('alert')).toHaveTextContent('group-wide access')
  })

  it('offers no group list to move, and no move to the group, without group-wide access', async () => {
    switchOn()
    prices.getFacilityPriceLists.mockResolvedValue(lists({ group_wide: false }))
    render(<FacilitiesPanel />)
    const section = await openPrices(2)
    const select = await within(section).findByLabelText('Default price list') as HTMLSelectElement
    expect(Array.from(select.options).map(o => o.value)).toEqual(['', '11'])
    expect(within(section).queryByRole('button', { name: /Make group-wide/ })).toBeNull()
  })

  it('makes a facility’s list group-wide and reloads', async () => {
    switchOn()
    prices.setPriceListAppliesTo.mockResolvedValue({ plan: { ...OWN, facility_id: null }, changed: true })
    render(<FacilitiesPanel />)
    const section = await openPrices(2)
    fireEvent.click(await within(section).findByRole('button', { name: 'Make group-wide Annexe rates (ANX-RATES)' }))
    await waitFor(() => expect(prices.setPriceListAppliesTo).toHaveBeenCalledWith(11, null))
    await waitFor(() => expect(prices.getFacilityPriceLists).toHaveBeenCalledTimes(2))
  })

  it('shows no editor to an account that may not change price lists', async () => {
    switchOn()
    prices.getFacilityPriceLists.mockResolvedValue(lists({ can_edit: false }))
    render(<FacilitiesPanel />)
    const section = await openPrices(2)
    await within(section).findByTestId('facility-price-default-2')
    expect(within(section).queryByLabelText('Default price list')).toBeNull()
  })

  it('hides itself when the server answers 404', async () => {
    switchOn()
    prices.getFacilityPriceLists.mockRejectedValue(Object.assign(new Error('off'), {
      status: 404, detail: { error: 'module_disabled' },
    }))
    render(<FacilitiesPanel />)
    await openPrices(1)
    await waitFor(() => expect(screen.queryByTestId('facility-price-lists-1')).toBeNull())
  })
})
