/**
 * Pharmacy store, NDPS register (grn-13): what the screen must get right.
 *
 *  1. The view registers as 'ndps', order 80, behind pharmacy_store.ndps.
 *  2. The switch is said in words: off (and asked for but off, ruling GRN-RS7
 *     held), on with each failure mode. Practice entries carry their honesty
 *     label. With the register off nothing offers to write.
 *  3. Balances show the register against the shelf with a status per drug;
 *     "Check against the shelf" raises variances and says how many.
 *  4. Opening the register asks for a reason, a witness and their PIN before
 *     anything is sent, and says how many old differences it raised as
 *     variances.
 *  5. The register lists entries newest first with the patient's hospital
 *     number and the witness; the export goes through the download call with
 *     the dates, and a malformed date is refused before any request.
 *  6. A variance is reconciled only with a note, a witness and a PIN; one is
 *     reported to the incident register in one click.
 *  7. A 'module_disabled' answer hands back to the panel.
 *
 * Driven through a mocked apiFetch (and fetch for the export), so the real
 * src/api/pharmacyStore files build the URLs and bodies.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import NdpsView, { viewId, viewLabel, viewOrder, viewPermission } from './NdpsView'

const apiFetch = vi.fn()

vi.mock('../../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
  errorDetailMessage: (_d: unknown, status: number) => `Request failed (${status})`,
  handleResponseError: async () => { throw new Error('x') },
  getUser: () => ({ id: 'PHA2', name: 'Test user', role: 'pharmacist' }),
}))

function options(overrides: Record<string, unknown> = {}) {
  return {
    register_on: true, failure_mode: 'record', requested: true, lane: 'live',
    lane_label: 'Live register',
    stores: [{ id: 3, code: 'VAULT', name: 'NDPS vault', pharmacy_kind: 'ndps_vault' }],
    drugs: [{ id: 'MED-MOR', name: 'Morphine 10mg Injection', strength: '10mg',
      form: 'Injection', ndps_category: 'narcotic' }],
    witnesses: [{ id: 'PHA3', name: 'Pharmacist Three' }],
    may_ndps: true, entry_labels: {}, variance_labels: {}, ...overrides,
  }
}

const BALANCES = {
  lane: 'live', lane_label: 'Live register', clean: false, unplaced_units: 0,
  rows: [
    { store_id: 3, store_code: 'VAULT', store_name: 'NDPS vault', store_kind: 'ndps_vault',
      drug_id: 'MED-MOR', drug_name: 'Morphine 10mg Injection', strength: '10mg',
      form: 'Injection', ndps_category: 'narcotic', controlled: true, register_units: 9,
      shelf_units: 9, difference_units: 0, opened_at: '2026-10-01T08:00:00Z', opening_seq: 1,
      entries_since_opening: 3, status: 'matches', status_label: 'Matches the shelf',
      open_variances: 0 },
    { store_id: 3, store_code: 'VAULT', store_name: 'NDPS vault', store_kind: 'ndps_vault',
      drug_id: 'MED-FEN', drug_name: 'Fentanyl 50mcg Injection', strength: '50mcg',
      form: 'Injection', ndps_category: 'narcotic', controlled: true, register_units: null,
      shelf_units: 4, difference_units: null, opened_at: null, opening_seq: null,
      entries_since_opening: 0, status: 'not_opened', status_label: 'Register not opened',
      open_variances: 1 },
  ],
}

const REGISTER = {
  lane: 'live', lane_label: 'Live register', next_before_id: null,
  entries: [
    { id: 40, seq: 4, lane: 'live', lane_label: 'Live register', store_id: 3,
      store_name: 'NDPS vault', drug_id: 'MED-MOR', drug_name: 'Morphine 10mg Injection',
      strength: '10mg', form: 'Injection', lot_id: 'LOT-1', batch_no: 'MB-1',
      entry_type: 'dispense', entry_label: 'Dispensed to a patient', quantity_units: -1,
      balance_after_units: 9, counterparty_type: 'patient', counterparty: 'Asha Rao',
      patient_id: 'P1', patient_name: 'Asha Rao', patient_mrn: 'MRN-77',
      patient_in_scope: true, helix_order_id: 'ORD-1', prescriber_name: 'Dr. Mehta',
      document_ref: 'helix_order:ORD-1', movement_ref: 'MV-1', actor_id: 'PHA2',
      actor_name: 'Test user', witness_id: null, witness_name: null,
      occurred_at: '2026-10-09T05:00:00Z', recorded_at: null, remarks: null },
    { id: 39, seq: 3, lane: 'live', lane_label: 'Live register', store_id: 3,
      store_name: 'NDPS vault', drug_id: 'MED-MOR', drug_name: 'Morphine 10mg Injection',
      strength: '10mg', form: 'Injection', lot_id: 'LOT-1', batch_no: 'MB-1',
      entry_type: 'wastage', entry_label: 'Wastage', quantity_units: -1,
      balance_after_units: 10, counterparty_type: 'none', counterparty: '',
      patient_id: null, patient_name: null, patient_mrn: null, patient_in_scope: null,
      helix_order_id: null, prescriber_name: null, document_ref: null, movement_ref: 'MV-0',
      actor_id: 'PHA2', actor_name: 'Test user', witness_id: 'PHA3',
      witness_name: 'Pharmacist Three', occurred_at: '2026-10-08T05:00:00Z',
      recorded_at: null, remarks: 'Ampoule broke' },
  ],
}

function variance(overrides: Record<string, unknown> = {}) {
  return {
    id: 5, lane: 'live', lane_label: 'Live register', store_id: 3, store_name: 'NDPS vault',
    helix_medication_id: 'MED-FEN', drug_name: 'Fentanyl 50mcg Injection', lot_id: null,
    helix_order_id: 'ORD-9', movement_ref: 'MV-9', variance_kind: 'register_write_failed',
    variance_label: 'The register could not record a movement', expected_units: 3,
    found_units: 3, variance_units: 0, detail: 'Register disk fault', status: 'open',
    raised_by: 'PHA2', raised_by_name: 'Test user', raised_at: '2026-10-09T05:10:00Z',
    reconciled_by: null, reconciled_by_name: null, reconciled_at: null,
    reconciliation_note: null, incident_ref: null, ...overrides,
  }
}

let currentOptions = options()

function routeApi(overrides: Record<string, (path: string, init?: RequestInit) => unknown> = {}) {
  apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
    for (const [prefix, handler] of Object.entries(overrides)) {
      if (path.startsWith(prefix)) return handler(path, init)
    }
    if (path.startsWith('/pharmacy-store/ndps/options')) return currentOptions
    if (path.startsWith('/pharmacy-store/ndps/balances')) return BALANCES
    if (path.startsWith('/pharmacy-store/ndps/register')) return REGISTER
    if (path.startsWith('/pharmacy-store/ndps/variances') && init?.method === 'POST') {
      if (path.endsWith('/incident')) return { incident_ref: 'INC-2026-0042', incident_id: 42 }
      return { variance: variance({ status: 'reconciled' }) }
    }
    if (path.startsWith('/pharmacy-store/ndps/variances')) return { lane: 'live', variances: [variance()] }
    if (path.startsWith('/pharmacy-store/ndps/reconcile')) {
      return { raised: [{ variance_id: 6, store_id: 3, drug_id: 'MED-MOR' }], balances: BALANCES }
    }
    if (path.startsWith('/pharmacy-store/ndps/openings')) {
      return { opened: [{ entry_id: 50, store_id: 3, drug_id: 'MED-FEN', units: 4,
        previous_register_units: null, previous_status: 'not_opened', variance_id: null,
        variance_raised: false }], balances: BALANCES }
    }
    throw new Error(`unexpected ${path}`)
  })
}

function posts(prefix: string) {
  return apiFetch.mock.calls.filter(([path, init]) =>
    String(path).startsWith(prefix) && (init as RequestInit | undefined)?.method === 'POST')
}

function body(call: unknown[]) {
  return JSON.parse(String((call[1] as RequestInit).body))
}

function renderView() {
  const onModuleOff = vi.fn()
  render(<NdpsView meta={{}} openView={() => {}} onModuleOff={onModuleOff} />)
  return { onModuleOff }
}

beforeEach(() => {
  currentOptions = options()
  apiFetch.mockReset()
  routeApi()
})

afterEach(() => {
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe('NdpsView', () => {
  it('registers as the NDPS register behind the NDPS permission', () => {
    expect(viewId).toBe('ndps')
    expect(viewLabel).toBe('NDPS register')
    expect(viewOrder).toBe(80)
    expect(viewPermission).toBe('pharmacy_store.ndps')
  })

  it('says the register is off, and offers nothing that writes', async () => {
    currentOptions = options({ register_on: false, failure_mode: null, requested: true })
    renderView()
    expect(await screen.findByText(/was asked for on this site but stays off/)).toBeInTheDocument()
    await screen.findByText('Morphine 10mg Injection')
    expect(screen.queryByRole('button', { name: 'Check against the shelf' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Open the register' })).toBeNull()
  })

  it('names the record mode and labels practice entries', async () => {
    currentOptions = options({ lane: 'non_production', lane_label: 'Practice entries' })
    renderView()
    expect(await screen.findByText(/the dispense goes ahead and a variance is raised/))
      .toBeInTheDocument()
    expect(screen.getByText(/Practice entries\. This computer is not set up/)).toBeInTheDocument()
  })

  it('shows each balance with its status and checks against the shelf', async () => {
    renderView()
    expect(await screen.findByText('Matches the shelf')).toBeInTheDocument()
    expect(screen.getByText('Register not opened')).toBeInTheDocument()
    expect(screen.getByText('1 open variance')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Check against the shelf' }))
    expect(await screen.findByText('Checked. 1 difference raised as variances for reconciliation.'))
      .toBeInTheDocument()
    expect(posts('/pharmacy-store/ndps/reconcile')).toHaveLength(1)
  })

  it('opens the register only with a reason, a witness and a PIN', async () => {
    renderView()
    await screen.findByText('Matches the shelf')
    fireEvent.click(screen.getByRole('button', { name: 'Open the register' }))
    const form = screen.getByLabelText('Witness').closest('form') as HTMLFormElement
    fireEvent.click(within(form).getByRole('button', { name: 'Open the register' }))
    expect(await screen.findByText('Say why the register is being opened.')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Why is the register being opened?'),
      { target: { value: 'Register switched on today' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Open the register' }))
    expect(await screen.findByText('Choose the pharmacist who witnesses this.')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Witness'), { target: { value: 'PHA3' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Open the register' }))
    expect(await screen.findByText('The witness enters their signing PIN.')).toBeInTheDocument()
    expect(posts('/pharmacy-store/ndps/openings')).toHaveLength(0)
    fireEvent.change(screen.getByLabelText('Witness signing PIN'), { target: { value: '5173' } })
    fireEvent.change(screen.getByLabelText('Store'), { target: { value: '3' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Open the register' }))
    expect(await screen.findByText('Register opened from the shelf for 1 drug.')).toBeInTheDocument()
    expect(body(posts('/pharmacy-store/ndps/openings')[0])).toEqual({
      reason: 'Register switched on today', witness_id: 'PHA3', witness_pin: '5173', store_id: 3,
    })
  })

  it('says when opening again raised the old difference as a variance', async () => {
    routeApi({
      '/pharmacy-store/ndps/openings': () => ({
        opened: [
          { entry_id: 51, store_id: 3, drug_id: 'MED-MOR', units: 3, previous_register_units: 5,
            previous_status: 'differs', variance_id: 7, variance_raised: true },
          { entry_id: 52, store_id: 3, drug_id: 'MED-FEN', units: 4, previous_register_units: 4,
            previous_status: 'matches', variance_id: null, variance_raised: false },
        ],
        balances: BALANCES,
      }),
    })
    renderView()
    await screen.findByText('Matches the shelf')
    fireEvent.click(screen.getByRole('button', { name: 'Open the register' }))
    expect(screen.getByText(/that difference is raised as a variance before the register opens again/))
      .toBeInTheDocument()
    const form = screen.getByLabelText('Witness').closest('form') as HTMLFormElement
    fireEvent.change(screen.getByLabelText('Why is the register being opened?'),
      { target: { value: 'Register back on after an outage' } })
    fireEvent.change(screen.getByLabelText('Witness'), { target: { value: 'PHA3' } })
    fireEvent.change(screen.getByLabelText('Witness signing PIN'), { target: { value: '5173' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Open the register' }))
    expect(await screen.findByText('Register opened from the shelf for 2 drugs. 1 difference from '
      + 'the old register raised as variances for reconciliation.')).toBeInTheDocument()
  })

  it('lists entries and exports the register with the dates', async () => {
    const fetchMock = vi.fn(async () => new Response('Entry,Time (UTC)\n', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const createUrl = vi.fn(() => 'blob:ndps')
    const saved = { create: URL.createObjectURL, revoke: URL.revokeObjectURL }
    URL.createObjectURL = createUrl
    URL.revokeObjectURL = vi.fn()
    try {
      await exportFlow(fetchMock, createUrl)
    } finally {
      URL.createObjectURL = saved.create
      URL.revokeObjectURL = saved.revoke
    }
  })

  async function exportFlow(fetchMock: ReturnType<typeof vi.fn>, createUrl: ReturnType<typeof vi.fn>) {
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Register' }))
    expect(await screen.findByText('Dispensed to a patient')).toBeInTheDocument()
    expect(screen.getByText('Hospital number MRN-77')).toBeInTheDocument()
    expect(screen.getByText('Witness Pharmacist Three')).toBeInTheDocument()
    expect(screen.getAllByText('-1', { selector: 'td' })).toHaveLength(2)
    fireEvent.change(screen.getByLabelText('First date'), { target: { value: '2026-10-01' } })
    fireEvent.click(screen.getByRole('button', { name: 'Export as a spreadsheet file' }))
    expect(await screen.findByText('Export ready. The export is recorded in the audit log.'))
      .toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const url = String((fetchMock.mock.calls[0] as unknown[])[0])
    expect(url).toBe('/api/pharmacy-store/ndps/export.csv?from=2026-10-01')
    expect(createUrl).toHaveBeenCalled()
  }

  it('reconciles a variance only with a note, a witness and a PIN, and reports one', async () => {
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Variances' }))
    expect(await screen.findByText(/The register could not record a movement/)).toBeInTheDocument()
    expect(screen.getByText('Medicine order ORD-9')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Reconcile' }))
    fireEvent.click(screen.getByRole('button', { name: 'Record the reconciliation' }))
    expect(await screen.findByText('Say what was found and what was done.')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('What was found and done?'),
      { target: { value: 'Entry written by hand, register opened again' } })
    fireEvent.change(screen.getByLabelText('Witness'), { target: { value: 'PHA3' } })
    fireEvent.change(screen.getByLabelText('Witness signing PIN'), { target: { value: '5173' } })
    fireEvent.click(screen.getByRole('button', { name: 'Record the reconciliation' }))
    expect(await screen.findByText('Variance reconciled.')).toBeInTheDocument()
    expect(body(posts('/pharmacy-store/ndps/variances/5/reconcile')[0])).toEqual({
      note: 'Entry written by hand, register opened again', witness_id: 'PHA3', witness_pin: '5173',
    })
    fireEvent.click(screen.getByRole('button', { name: 'Report as an incident' }))
    expect(await screen.findByText('Reported to the incident register as INC-2026-0042.'))
      .toBeInTheDocument()
  })

  it('hands back to the panel when the module is switched off', async () => {
    routeApi({
      '/pharmacy-store/ndps/options': () => {
        throw Object.assign(new Error('Not found'), {
          status: 404,
          detail: { error: 'module_disabled', module: 'pharmacy-store', message: 'Off' },
        })
      },
    })
    const { onModuleOff } = renderView()
    await waitFor(() => expect(onModuleOff).toHaveBeenCalled())
  })
})
