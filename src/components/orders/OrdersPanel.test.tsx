/**
 * Orders: the order form must say whose chart it is writing on, must not carry
 * a half-written order across a patient switch, must say that a medication
 * order reaches no nurse or pharmacist, and the list must show what an order
 * set just placed.
 */
import { render, screen, waitFor, cleanup, fireEvent, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const PATIENTS = [
  { id: 'P-SURESH', name: 'Suresh Reddy', mrn: 'MRN-GM004', bed: 'GM-104', ward: 'GM-1', age: 55, sex: 'M' },
  { id: 'P-ANANYA', name: 'Ananya Kapoor', mrn: 'MRN-GM001', bed: 'GM-101', ward: 'GM-1', age: 34, sex: 'F' },
]

// What /orders/patient/<id> returns, per patient. Mutated by the apply test.
let ordersByPatient: Record<string, unknown[]> = {}

const apiFetch = vi.fn(async (path: string) => {
  const m = /^\/orders\/patient\/(.+)$/.exec(path)
  if (m) return { orders: ordersByPatient[m[1]] ?? [] }
  return {}
})

vi.mock('../../api/client', () => ({
  apiFetch: (path: string) => apiFetch(path),
  createOrder: vi.fn(async () => ({ id: 'O-NEW' })),
  getPatients: async () => PATIENTS,
  isSafetyAckRequired: () => false,
}))

const SET = {
  id: 'sepsis-six', name: 'Sepsis Six (1-hour bundle)', indication: 'Suspected sepsis',
  guideline: { name: 'Surviving Sepsis Campaign 2021' }, item_count: 1, fingerprint: 'fp1',
}

const applyOrderSet = vi.fn(async () => {
  ordersByPatient['P-SURESH'] = [{
    id: 'O-1', patient_id: 'P-SURESH', order_type: 'lab', code: null, code_system: null,
    description: 'Serum lactate', priority: 'stat', status: 'draft', source: 'order_set',
    result_text: null, result_at: null, created_at: '2026-10-03 09:00:00',
  }]
  return { status: 'applied', orders: [{ order_type: 'lab' }] }
})

vi.mock('../../api/orderSets', () => ({
  listOrderSets: async () => ({ sets: [SET], config: null }),
  previewOrderSet: async (_s: string, patientId: string) => ({
    patient_id: patientId,
    set: { id: SET.id, name: SET.name, guideline: {}, fingerprint: 'fp1' },
    items: [{
      index: 0, order_type: 'lab', description: 'Serum lactate', code: null, code_system: null,
      priority: 'stat', drug_name: null, dose: null, route: null, frequency: null, note: null,
      edited_fields: [], medication_safety: null, dose_check: null,
      blocked: false, block_kind: null, block_reason: null,
    }],
    blocked: [],
    would_apply: true,
    unknown_codes: [],
    recent_application: null,
    patient: { ...PATIENTS.find(p => p.id === patientId), allergies: [] },
  }),
  applyOrderSet: () => applyOrderSet(),
  listOrderSetApplications: async () => ({ applications: [] }),
}))

// lis-7: the laboratory's cancel and catalogue.
const cancelLabOrder = vi.fn(async () => ({ order: { id: 'O-LAB', status: 'cancelled' } }))
vi.mock('../../api/lab/orders', () => ({
  cancelLabOrder: (...a: unknown[]) => cancelLabOrder(...(a as [])),
}))
const searchLabCatalogue = vi.fn(async () => ({ tests: [] as unknown[] }))
vi.mock('../../api/lab/master', () => ({
  searchLabCatalogue: (...a: unknown[]) => searchLabCatalogue(...(a as [])),
}))

import { createOrder } from '../../api/client'
import { resetModuleFlags, setModuleFlags } from '../../shellConfig'
import OrdersPanel from './OrdersPanel'

const pick = async (name: string) => {
  const input = await screen.findByPlaceholderText(/name, bed or MRN/i)
  fireEvent.focus(input)
  fireEvent.change(input, { target: { value: name.split(' ')[0] } })
  const list = await screen.findByRole('listbox')
  fireEvent.click(within(list).getByText(name).closest('button')!)
}

beforeEach(() => {
  ordersByPatient = {}
  apiFetch.mockClear()
  applyOrderSet.mockClear()
})
afterEach(cleanup)

describe('order form', () => {
  it('says a medication order is not sent to the ward or pharmacy', async () => {
    render(<OrdersPanel />)
    await pick('Suresh Reddy')
    fireEvent.click(screen.getByRole('button', { name: '+ New Order' }))

    const type = screen.getByDisplayValue('lab')
    expect(screen.queryByText(/Not sent to the ward or pharmacy/)).toBeNull()
    fireEvent.change(type, { target: { value: 'medication' } })
    expect(screen.getByRole('note').textContent).toBe(
      'Not sent to the ward or pharmacy. Prescribe from the patient chart (Medications) so nurses can give it.')
  })

  it('names the patient and starts blank after switching patient', async () => {
    render(<OrdersPanel />)
    await pick('Suresh Reddy')
    fireEvent.click(screen.getByRole('button', { name: '+ New Order' }))
    await waitFor(() => expect(screen.getByText(/Ordering for/).textContent)
      .toBe('Ordering for Suresh Reddy, bed GM-104, GM-1'))

    const desc = screen.getByPlaceholderText('Order description') as HTMLInputElement
    fireEvent.change(desc, { target: { value: 'Blood cultures x2' } })
    expect(desc.value).toBe('Blood cultures x2')

    await pick('Ananya Kapoor')
    await waitFor(() => expect(screen.getByText(/Ordering for/).textContent)
      .toBe('Ordering for Ananya Kapoor, bed GM-101, GM-1'))
    expect((screen.getByPlaceholderText('Order description') as HTMLInputElement).value).toBe('')
  })
})

describe('order list', () => {
  it('marks medication orders as not sent to the ward or pharmacy', async () => {
    ordersByPatient['P-SURESH'] = [
      { id: 'O-M', patient_id: 'P-SURESH', order_type: 'medication', code: null, code_system: null,
        description: 'Ceftriaxone 1 g IV', priority: 'stat', status: 'draft', source: 'clinician',
        result_text: null, result_at: null, created_at: '2026-10-03 09:00:00' },
      { id: 'O-L', patient_id: 'P-SURESH', order_type: 'lab', code: null, code_system: null,
        description: 'Serum lactate', priority: 'stat', status: 'draft', source: 'clinician',
        result_text: null, result_at: null, created_at: '2026-10-03 09:00:00' },
    ]
    render(<OrdersPanel />)
    await pick('Suresh Reddy')
    await screen.findByText('Ceftriaxone 1 g IV')
    expect(screen.getAllByText(/Not sent to the ward or pharmacy/)).toHaveLength(1)
  })

  it('shows the orders an order set just placed when you come back to the list', async () => {
    render(<OrdersPanel />)
    await pick('Suresh Reddy')
    await screen.findByText('No orders yet')

    fireEvent.click(screen.getByRole('tab', { name: 'Order sets' }))
    // The shared selection: the protocol screen already knows the patient.
    fireEvent.click((await screen.findByText('Sepsis Six (1-hour bundle)')).closest('button')!)
    fireEvent.click(await screen.findByRole('button', { name: 'Apply 1 order for Suresh Reddy (GM-104)' }))
    await waitFor(() => expect(applyOrderSet).toHaveBeenCalledTimes(1))

    fireEvent.click(screen.getByRole('tab', { name: 'Individual orders' }))
    expect(await screen.findByText('Serum lactate')).toBeTruthy()
    expect(screen.queryByText('No orders yet')).toBeNull()
  })
})

describe('laboratory orders (lis-7)', () => {
  const LAB_MANAGED = {
    id: 'O-LAB', patient_id: 'P-SURESH', order_type: 'lab', code: '58410-2', code_system: 'LOINC',
    description: 'Complete blood count', priority: 'routine', status: 'in_progress',
    source: 'clinician', result_text: null, result_at: null, created_at: '2026-10-04 08:00:00',
    lab_managed: true,
    lab: {
      tests: [
        { test_id: 'LOINC:718-7', name: 'Haemoglobin', short_name: 'Haemoglobin', status: 'released',
          status_text: 'Reported', specimen_id: '261004000127' },
        { test_id: 'LOINC:6690-2', name: 'WBC', short_name: 'WBC', status: 'collected',
          status_text: 'Collected', specimen_id: '261004000127' },
      ],
      counts: { Reported: 1, Collected: 1 },
      summary: '2 tests: 1 reported, 1 collected',
      rejected: 0,
      cancelled: null,
    },
  }
  const FREE_TEXT_LAB = {
    id: 'O-FREE', patient_id: 'P-SURESH', order_type: 'lab', code: '2823-3', code_system: 'LOINC',
    description: 'Serum potassium', priority: 'routine', status: 'active', source: 'clinician',
    result_text: null, result_at: null, created_at: '2026-10-04 08:00:00', lab_managed: false,
  }

  beforeEach(() => {
    cancelLabOrder.mockClear()
    searchLabCatalogue.mockClear()
    vi.mocked(createOrder).mockClear()
  })
  afterEach(() => resetModuleFlags())

  it('shows where each test is and no next-status or + Result for a lab-managed order', async () => {
    ordersByPatient['P-SURESH'] = [LAB_MANAGED, FREE_TEXT_LAB]
    render(<OrdersPanel />)
    await pick('Suresh Reddy')
    await screen.findByText('Complete blood count')
    // The title sits in the card's main column: title, then column, then card.
    const card = screen.getByText('Complete blood count').closest('div')!.parentElement!.parentElement!
    expect(within(card).getByText('Laboratory: 2 tests: 1 reported, 1 collected')).toBeTruthy()
    expect(within(card).getByText('Haemoglobin: Reported')).toBeTruthy()
    expect(within(card).queryByRole('button', { name: /→/ })).toBeNull()
    expect(within(card).queryByRole('button', { name: '+ Result' })).toBeNull()
    expect(within(card).getByRole('button', { name: 'Cancel' })).toBeTruthy()

    // The order the laboratory does not manage keeps today's buttons.
    const free = screen.getByText('Serum potassium').closest('div')!.parentElement!.parentElement!
    expect(within(free).getByRole('button', { name: '→ in progress' })).toBeTruthy()
    expect(within(free).getByRole('button', { name: '+ Result' })).toBeTruthy()
  })

  it('cancels a lab-managed order through the laboratory, with a required reason', async () => {
    ordersByPatient['P-SURESH'] = [LAB_MANAGED]
    render(<OrdersPanel />)
    await pick('Suresh Reddy')
    await screen.findByText('Complete blood count')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    const reason = screen.getByLabelText('Cancellation reason for Complete blood count')
    expect((reason as HTMLInputElement).placeholder).toBe('Reason (required)')
    const confirm = screen.getByRole('button', { name: 'Confirm cancel' }) as HTMLButtonElement
    expect(confirm.disabled).toBe(true)
    fireEvent.change(reason, { target: { value: 'Duplicate order' } })
    expect(confirm.disabled).toBe(false)
    fireEvent.click(confirm)
    await waitFor(() => expect(cancelLabOrder).toHaveBeenCalledWith('O-LAB', 'Duplicate order'))
    // Never through the Orders screen's own transition.
    expect(apiFetch.mock.calls.some(([path]) => String(path).includes('/transition'))).toBe(false)
  })

  it('says why a lab order was not taken in', async () => {
    ordersByPatient['P-SURESH'] = [{ ...FREE_TEXT_LAB, lab_intake: { kind: 'unmapped_code',
      message: 'The laboratory could not take this order in: its code is not in the laboratory catalogue. The laboratory has been told.' } }]
    render(<OrdersPanel />)
    await pick('Suresh Reddy')
    expect(await screen.findByText(/could not take this order in/)).toBeTruthy()
  })

  it('offers the laboratory catalogue, never ICD-10, while the module is on', async () => {
    setModuleFlags({ lab: true })
    searchLabCatalogue.mockResolvedValue({ tests: [{
      id: 'LOINC:2823-3', code: '2823-3', code_system: 'LOINC', name: 'Potassium', short_name: 'Potassium',
      kind: 'analyte', performed_at: 'lab', unit: 'mmol/L', result_type: 'numeric', section_id: 'biochemistry',
      section_name: 'Biochemistry', specimen_type_code: 'serum', specimen_display: 'Serum',
      container: 'Gold top (SST)', container_colour_name: 'gold',
    }] })
    render(<OrdersPanel />)
    await pick('Suresh Reddy')
    fireEvent.click(screen.getByRole('button', { name: '+ New Order' }))
    expect(screen.queryByLabelText('Code System')).toBeNull()
    expect(screen.queryByText('ICD10')).toBeNull()
    fireEvent.change(screen.getByLabelText('Laboratory test'), { target: { value: 'pota' } })
    const option = await screen.findByRole('button', { name: /Potassium · LOINC 2823-3 · Gold top/ })
    fireEvent.click(option)
    expect(screen.getByText('Collect in: Gold top (SST), Serum')).toBeTruthy()
    expect((screen.getByPlaceholderText('Order description') as HTMLInputElement).value).toBe('Potassium')
    fireEvent.click(screen.getByRole('button', { name: 'Create Order' }))
    await waitFor(() => expect(createOrder).toHaveBeenCalledTimes(1))
    expect(vi.mocked(createOrder).mock.calls[0][0]).toMatchObject({
      order_type: 'lab', code: '2823-3', code_system: 'LOINC', description: 'Potassium' })
  })

  describe('the description follows the chosen test', () => {
    const entry = (code: string, name: string) => ({
      id: `LOINC:${code}`, code, code_system: 'LOINC', name, short_name: name,
      kind: 'analyte', performed_at: 'lab', unit: 'mmol/L', result_type: 'numeric', section_id: 'biochemistry',
      section_name: 'Biochemistry', specimen_type_code: 'serum', specimen_display: 'Serum',
      container: 'Gold top (SST)', container_colour_name: 'gold',
    })
    const description = () => screen.getByPlaceholderText('Order description') as HTMLInputElement
    const createButton = () => screen.getByRole('button', { name: 'Create Order' }) as HTMLButtonElement

    const openForm = async () => {
      setModuleFlags({ lab: true })
      searchLabCatalogue.mockImplementation(async (...a: unknown[]) => {
        const term = String(a[0]).toLowerCase()
        if (term.startsWith('pota')) return { tests: [entry('2823-3', 'Potassium')] }
        if (term.startsWith('sod')) return { tests: [entry('2951-2', 'Sodium')] }
        return { tests: [] }
      })
      render(<OrdersPanel />)
      await pick('Suresh Reddy')
      fireEvent.click(screen.getByRole('button', { name: '+ New Order' }))
    }
    const chooseTest = async (term: string, option: RegExp) => {
      fireEvent.change(screen.getByLabelText('Laboratory test'), { target: { value: term } })
      fireEvent.click(await screen.findByRole('button', { name: option }))
    }

    afterEach(() => { searchLabCatalogue.mockReset() })

    it('pick, Change, pick again: the description and the code name the same test', async () => {
      await openForm()
      await chooseTest('pota', /Potassium · LOINC 2823-3/)
      expect(description().value).toBe('Potassium')
      fireEvent.click(screen.getByRole('button', { name: 'Change' }))
      // Change empties the name it wrote, so nothing can be saved half-chosen.
      expect(description().value).toBe('')
      expect(createButton().disabled).toBe(true)
      await chooseTest('sod', /Sodium · LOINC 2951-2/)
      expect(description().value).toBe('Sodium')
      fireEvent.click(createButton())
      await waitFor(() => expect(createOrder).toHaveBeenCalledTimes(1))
      expect(vi.mocked(createOrder).mock.calls[0][0]).toMatchObject({
        order_type: 'lab', code: '2951-2', code_system: 'LOINC', description: 'Sodium' })
    })

    it('a description typed before picking is replaced by the chosen test', async () => {
      await openForm()
      fireEvent.change(description(), { target: { value: 'Potassium' } })
      await chooseTest('sod', /Sodium · LOINC 2951-2/)
      expect(description().value).toBe('Sodium')
      fireEvent.click(createButton())
      await waitFor(() => expect(createOrder).toHaveBeenCalledTimes(1))
      expect(vi.mocked(createOrder).mock.calls[0][0]).toMatchObject({ code: '2951-2', description: 'Sodium' })
    })

    it('Change keeps words the clinician added, and the next pick still renames the order', async () => {
      await openForm()
      await chooseTest('pota', /Potassium · LOINC 2823-3/)
      fireEvent.change(description(), { target: { value: 'Potassium, repeat after replacement' } })
      fireEvent.click(screen.getByRole('button', { name: 'Change' }))
      expect(description().value).toBe('Potassium, repeat after replacement')
      // No test is chosen, so the order cannot be created under that title.
      expect(createButton().disabled).toBe(true)
      await chooseTest('sod', /Sodium · LOINC 2951-2/)
      expect(description().value).toBe('Sodium')
    })

    it('leaving the laboratory order type drops the chosen test', async () => {
      await openForm()
      await chooseTest('pota', /Potassium · LOINC 2823-3/)
      const type = screen.getByLabelText('Order Type')
      fireEvent.change(type, { target: { value: 'imaging' } })
      expect(description().value).toBe('')
      fireEvent.change(type, { target: { value: 'lab' } })
      // Back on a laboratory order the test must be chosen again.
      expect(screen.queryByRole('button', { name: 'Change' })).toBeNull()
      fireEvent.change(description(), { target: { value: 'Chest X-ray' } })
      expect(createButton().disabled).toBe(true)
      expect(createOrder).not.toHaveBeenCalled()
    })
  })

  it('keeps the free code form while the module is off', async () => {
    render(<OrdersPanel />)
    await pick('Suresh Reddy')
    fireEvent.click(screen.getByRole('button', { name: '+ New Order' }))
    expect(screen.getByLabelText('Code System')).toBeTruthy()
    expect(screen.queryByLabelText('Laboratory test')).toBeNull()
    expect(searchLabCatalogue).not.toHaveBeenCalled()
  })
})
