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
