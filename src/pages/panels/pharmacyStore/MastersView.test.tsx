/**
 * Pharmacy store, Masters (grn-5): what the screen must get right.
 *
 *  1. An unclassified drug reads "Not classified", never "none", and the
 *     clinical identity is shown, never offered for editing.
 *  2. Saving drug details sends only the fields that changed, GST as basis
 *     points, through PATCH /drugs/{id}.
 *  3. A classification change that removes control says it was sent for
 *     approval, from the server's answer, not a guess.
 *  4. The approve tier decides; the requester is told another approver must,
 *     and may only withdraw. An approver without the write tier sees no save
 *     buttons.
 *  5. Creating the dispensing store reports the legacy lots it adopted; ward
 *     kinds are offered only with the ward-stock switch on.
 *  6. A vendor's licence status is shown in words.
 *  7. A 'module_disabled' answer hands back to the panel.
 *
 * Driven through a mocked apiFetch, so the real src/api/pharmacyStore files
 * build the URLs and bodies.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import MastersView, { viewId, viewLabel, viewOrder, viewPermission } from './MastersView'

const apiFetch = vi.fn()
let role = 'pharmacist'
let userId = 'PHA1'

vi.mock('../../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
  errorDetailMessage: (_d: unknown, status: number) => `Request failed (${status})`,
  handleResponseError: async () => { throw new Error('x') },
  getUser: () => ({ id: userId, name: 'Test user', role }),
}))

const OPTIONS = {
  drug_schedules: [
    { value: 'none', label: 'No schedule' }, { value: 'G', label: 'Schedule G' },
    { value: 'H', label: 'Schedule H' }, { value: 'H1', label: 'Schedule H1' },
    { value: 'X', label: 'Schedule X' },
  ],
  ndps_categories: [
    { value: 'none', label: 'Not controlled' }, { value: 'narcotic', label: 'Narcotic' },
    { value: 'psychotropic', label: 'Psychotropic' },
  ],
  storage_conditions: [
    { value: 'room_temperature', label: 'Room temperature' }, { value: 'cool', label: 'Cool place' },
    { value: 'refrigerated', label: 'Refrigerated, 2 to 8 °C' }, { value: 'frozen', label: 'Frozen' },
  ],
  store_kinds: [],
  not_classified: 'Not classified',
  not_configured: 'Not configured',
}

function drug(overrides: Record<string, unknown> = {}) {
  return {
    id: 'MED-PARA', name: 'Paracetamol 650mg Oral Tablet', generic_name: 'Paracetamol',
    form: 'Tablet', strength: '650mg', pack_size_units: null, purchase_uom: null,
    dispense_unit: null, hsn_code: null, gst_rate_bp: null, manufacturer: null,
    drug_schedule: null, ndps_category: null, is_essential_narcotic: 0, storage_condition: null,
    reorder_level_units: null, reorder_qty_units: null, lead_time_days: null, is_active: 1,
    identity_status: 'active', updated_by: null, updated_at: null,
    schedule_label: 'Not classified', ndps_label: 'Not classified',
    storage_label: 'Not configured', gst_label: 'Not configured', pending_requests: 0,
    ...overrides,
  }
}

const REQUEST = {
  id: 7, helix_medication_id: 'MED-MORPH', change_kind: 'classification_downgrade',
  field: 'ndps_category', from_value: 'narcotic', to_value: 'psychotropic', reason: 'Notified',
  requested_by: 'PHA1', requested_at: '2026-10-04 06:00:00', decision: 'pending',
  decided_by: null, decided_at: null, decision_reason: null, drug_name: 'Morphine 10mg',
  requested_by_name: 'Pharmacist One',
}

function routeApi(overrides: Record<string, (path: string, init?: RequestInit) => unknown> = {}) {
  apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
    for (const [prefix, handler] of Object.entries(overrides)) {
      if (path.startsWith(prefix)) return handler(path, init)
    }
    if (path.startsWith('/pharmacy-store/drugs')) return { drugs: [drug()], options: OPTIONS }
    if (path.startsWith('/pharmacy-store/classification-requests')) return { requests: [REQUEST] }
    if (path.startsWith('/pharmacy-store/stores')) return { stores: [], legacy_lots: 3 }
    if (path.startsWith('/pharmacy-store/par-levels')) return { par_levels: [] }
    if (path.startsWith('/pharmacy-store/vendors')) return { vendors: [] }
    throw new Error(`unexpected ${path}`)
  })
}

function calls(prefix: string, method?: string) {
  return apiFetch.mock.calls.filter(([path, init]) =>
    String(path).startsWith(prefix) && (!method || (init as RequestInit | undefined)?.method === method))
}

function renderView(meta: Record<string, unknown> = {}) {
  const onModuleOff = vi.fn()
  render(<MastersView meta={meta} openView={() => {}} onModuleOff={onModuleOff} />)
  return { onModuleOff }
}

beforeEach(() => {
  apiFetch.mockReset()
  role = 'pharmacist'
  userId = 'PHA1'
})

afterEach(() => { vi.restoreAllMocks() })

describe('MastersView', () => {
  it('declares its tab', () => {
    expect([viewId, viewLabel, viewOrder, viewPermission])
      .toEqual(['masters', 'Masters', 90, 'pharmacy_store.read'])
  })

  it('reads an unclassified drug as Not classified and never offers its identity', async () => {
    routeApi()
    renderView()
    const row = (await screen.findByText('Paracetamol 650mg Oral Tablet')).closest('tr')!
    expect(within(row).getAllByText('Not classified')).toHaveLength(2)
    expect(within(row).queryByText(/none/i)).toBeNull()
    fireEvent.click(within(row).getByRole('button', { name: 'Open' }))
    expect(await screen.findByText(/are not changed here/)).toBeInTheDocument()
    expect(screen.queryByLabelText(/generic name/i)).toBeNull()
    expect(screen.queryByLabelText(/^strength/i)).toBeNull()
  })

  it('saves only the changed drug details, with GST in basis points', async () => {
    routeApi({
      '/pharmacy-store/drugs/MED-PARA': () => ({ drug: drug({ gst_rate_bp: 500, gst_label: '5%' }) }),
    })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    fireEvent.change(screen.getByLabelText('GST rate (%)'), { target: { value: '5' } })
    fireEvent.change(screen.getByLabelText('Storage'), { target: { value: 'cool' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save details' }))
    await screen.findByText('Saved.')
    const [[path, init]] = calls('/pharmacy-store/drugs/MED-PARA', 'PATCH')
    expect(path).toBe('/pharmacy-store/drugs/MED-PARA')
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({
      gst_rate_bp: 500, storage_condition: 'cool',
    })
  })

  it('says a control-removing classification was sent for approval', async () => {
    routeApi({
      '/pharmacy-store/drugs': (path, init) => {
        if ((init as RequestInit | undefined)?.method === 'POST') {
          return { drug: drug({ drug_schedule: 'H1' }), applied: [],
            requested: [{ ...REQUEST, field: 'drug_schedule' }] }
        }
        return { drugs: [drug({ drug_schedule: 'H1', schedule_label: 'Schedule H1' })],
          options: OPTIONS }
      },
    })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    fireEvent.change(screen.getByLabelText('Schedule'), { target: { value: 'G' } })
    fireEvent.change(screen.getByLabelText(/^Reason/), { target: { value: 'Reclassified' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save classification' }))
    expect(await screen.findByText(/Sent for approval: Schedule/)).toBeInTheDocument()
    expect(screen.getByText(/another approver must approve it/)).toBeInTheDocument()
    const [[, init]] = calls('/pharmacy-store/drugs/MED-PARA/classification', 'POST')
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({
      drug_schedule: 'G', reason: 'Reclassified',
    })
  })

  it('lets the requester withdraw but never decide', async () => {
    routeApi()
    renderView()
    fireEvent.click(screen.getByRole('button', { name: 'Approvals' }))
    expect(await screen.findByText('Morphine 10mg')).toBeInTheDocument()
    expect(screen.getByText('NDPS class: Narcotic to Psychotropic')).toBeInTheDocument()
    expect(screen.getByText(/another approver must decide it/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Withdraw' })).toBeInTheDocument()
  })

  it('lets a different approver decide, without the write tier forms', async () => {
    role = 'admin'
    userId = 'ADM1'
    routeApi({
      '/pharmacy-store/classification-requests/7/decision': () => ({
        request: { ...REQUEST, decision: 'approved', decided_by: 'ADM1' } }),
    })
    renderView()
    fireEvent.click(screen.getByRole('button', { name: 'Approvals' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(screen.queryByText('Morphine 10mg')).toBeNull())
    const [[, init]] = calls('/pharmacy-store/classification-requests/7/decision', 'POST')
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({ decision: 'approved' })

    fireEvent.click(screen.getByRole('button', { name: 'Drugs' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    expect(screen.queryByRole('button', { name: 'Save details' })).toBeNull()
    expect(screen.getByLabelText('GST rate (%)')).toBeDisabled()
  })

  it('reports the legacy lots the dispensing store adopted', async () => {
    role = 'admin'
    routeApi({
      '/pharmacy-store/stores': (_p, init) => {
        if ((init as RequestInit | undefined)?.method === 'POST') {
          return {
            store: { id: 1, code: 'MAIN', name: 'Main pharmacy', pharmacy_kind: 'dispensing',
              kind_label: 'Dispensing pharmacy', department: null, ward: null,
              incharge_user_id: null, is_active: 1, units_on_hand: 40 },
            adoption: { store_id: 1, adopted: ['L1', 'L2', 'L3'], merged: [], conflicts: [] },
          }
        }
        return { stores: [], legacy_lots: 3 }
      },
    })
    renderView()
    fireEvent.click(screen.getByRole('button', { name: 'Stores' }))
    expect(await screen.findByText(/3 lots are not in any store yet/)).toBeInTheDocument()
    const kinds = Array.from((screen.getByLabelText('Kind') as HTMLSelectElement).options)
      .map(o => o.textContent)
    expect(kinds).toEqual(['Dispensing pharmacy', 'Sub-store', 'NDPS vault', 'Returns quarantine'])
    fireEvent.change(screen.getByLabelText('Code'), { target: { value: 'MAIN' } })
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Main pharmacy' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add store' }))
    expect(await screen.findByText(/3 lots moved into the dispensing store/)).toBeInTheDocument()
  })

  it('says why lots stayed out once the dispensing store exists', async () => {
    role = 'admin'
    routeApi({
      '/pharmacy-store/stores': () => ({
        stores: [{ id: 1, code: 'MAIN', name: 'Main pharmacy', pharmacy_kind: 'dispensing',
          kind_label: 'Dispensing pharmacy', department: null, ward: null,
          incharge_user_id: null, is_active: 1, units_on_hand: 40 }],
        legacy_lots: 2,
      }),
    })
    renderView()
    fireEvent.click(screen.getByRole('button', { name: 'Stores' }))
    expect(await screen.findByText(/2 lots were left out of the dispensing store/)).toBeInTheDocument()
    expect(screen.queryByText(/Creating the dispensing store moves them/)).toBeNull()
  })

  it('offers ward kinds only with the ward-stock switch on', async () => {
    role = 'admin'
    routeApi()
    renderView({ features: { pharmacy_ward_stock: true } })
    fireEvent.click(screen.getByRole('button', { name: 'Stores' }))
    await screen.findByText(/not in any store yet/)
    const kinds = Array.from((screen.getByLabelText('Kind') as HTMLSelectElement).options)
      .map(o => o.textContent)
    expect(kinds).toContain('Ward stock')
    expect(kinds).toContain('Crash cart')
  })

  it('shows a vendor licence status in words', async () => {
    routeApi({
      '/pharmacy-store/vendors': () => ({ vendors: [{
        id: 1, code: 'LIFE', name: 'Lifeline Distributors', vendor_type: 'distributor',
        gstin: null, contact_name: null, contact_phone: null, is_active: 1, is_blacklisted: 0,
        drug_licence_numbers: 'KA-B1-1', drug_licence_valid_to: '2026-10-20',
        ndps_licence_number: null, licence_status: 'expiring' }] }),
    })
    renderView()
    fireEvent.click(screen.getByRole('button', { name: 'Vendors' }))
    expect(await screen.findByText('Licence expiring soon')).toBeInTheDocument()
    expect(screen.getByText('Valid to 20 Oct 2026')).toBeInTheDocument()
  })

  it('hands a switched-off module back to the panel', async () => {
    apiFetch.mockImplementation(async () => {
      throw Object.assign(new Error('switched off'), {
        status: 404, detail: { error: 'module_disabled', message: 'switched off' } })
    })
    const { onModuleOff } = renderView()
    await waitFor(() => expect(onModuleOff).toHaveBeenCalled())
  })
})
