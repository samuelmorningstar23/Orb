/**
 * Orders with the medication-order lifecycle (medorders-7): a CPOE or
 * order-set medicine is a draft a prescriber signs, and the screen says where
 * it is. Pinned here:
 *   * lifecycle off: the form and the list say the medicine is not sent to
 *     the ward or pharmacy, and nothing offers Sign;
 *   * lifecycle on: the form asks for the medicine as fields and places it
 *     with them; a draft says it waits for a signature, a prescriber gets Sign
 *     and "Awaiting my signature", a nurse gets neither; the next-status
 *     button is gone; Cancel needs a reason and is offered only before
 *     signing; a 428 at sign opens the acknowledgement and re-signs with it;
 *     a medicine not on the formulary says the pharmacy does not supply it.
 */
import { render, screen, waitFor, cleanup, fireEvent, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const PATIENTS = [
  { id: 'P-SURESH', name: 'Suresh Reddy', mrn: 'MRN-GM004', bed: 'GM-104', ward: 'GM-1', age: 55, sex: 'M' },
]

let role = 'doctor'
let orders: unknown[] = []

const apiFetch = vi.fn(async (path: string, _init?: RequestInit) => {
  if (/^\/orders\/patient\//.test(path)) return { orders }
  return { status: 'success' }
})

vi.mock('../../api/client', () => ({
  apiFetch: (path: string, init?: RequestInit) => apiFetch(path, init),
  createOrder: vi.fn(async () => ({ status: 'success', order: { id: 'O-NEW' } })),
  getPatients: async () => PATIENTS,
  isSafetyAckRequired: (e: unknown) => (e as { code?: string } | null)?.code === 'safety_ack_required',
  getUser: () => ({ role }),
  socket: { on: vi.fn(), off: vi.fn() },
}))

const signMedOrder = vi.fn(async (..._a: unknown[]) => ({
  order: {}, medication_id: 9, safety: null, duplicates: [], replayed: false,
}))
const createCpoeMedicationOrder = vi.fn(async (..._a: unknown[]) => ({
  status: 'success', order: { id: 'O-NEW' }, safety: { max_severity: 'none', interaction_screened: true },
}))
vi.mock('../../api/medorders', async importOriginal => ({
  ...(await importOriginal<typeof import('../../api/medorders')>()),
  signMedOrder: (...a: unknown[]) => signMedOrder(...a),
  createCpoeMedicationOrder: (...a: unknown[]) => createCpoeMedicationOrder(...a),
}))

vi.mock('../../api/lab/orders', () => ({ cancelLabOrder: vi.fn() }))
vi.mock('../../api/lab/master', () => ({ searchLabCatalogue: vi.fn(async () => ({ tests: [] })) }))

import { resetFeatureFlags, setFeatureFlags } from '../../shellConfig'
import OrdersPanel from './OrdersPanel'

function linkedOrder(over: Record<string, unknown> = {}, med: Record<string, unknown> = {}) {
  return {
    id: 'O-MED', patient_id: 'P-SURESH', order_type: 'medication', code: null, code_system: null,
    description: 'Ceftriaxone 1 g IV once daily', priority: 'urgent', status: 'draft',
    source: 'clinician', result_text: null, result_at: null, created_at: '2026-10-09 08:00:00',
    managed_by_medication_order: true,
    medication_order: {
      id: 'MO-1', status: 'draft', version: 1, source: 'cpoe', drug_name: 'Ceftriaxone',
      dose: '1 g', route: 'IV', frequency: 'once daily', is_prn: false, prn_indication: null,
      priority: 'urgent', entered_by: 'N001', entered_by_role: 'nurse', entered_by_name: 'Nurse Asha',
      prescriber_id: null, prescriber_name: null, prescribed_at: null, verified_by: null,
      verified_at: null, status_reason: null, status_changed_at: null, legacy_medication_id: null,
      needs_verification: false, created_at: '2026-10-09T08:00:00.000000+00:00',
      waiting_over_24h: false, on_formulary: true, ...med,
    },
    ...over,
  }
}

const NURSING = {
  id: 'O-NURSE', patient_id: 'P-SURESH', order_type: 'nursing', code: null, code_system: null,
  description: 'Hourly observations', priority: 'routine', status: 'draft', source: 'clinician',
  result_text: null, result_at: null, created_at: '2026-10-09 07:00:00',
}

const lifecycle = (on: boolean) =>
  setFeatureFlags({ med_order_store: on, med_order_lifecycle: on })

beforeEach(() => {
  role = 'doctor'
  orders = []
  apiFetch.mockClear()
  signMedOrder.mockClear()
  createCpoeMedicationOrder.mockClear()
  resetFeatureFlags()
})
afterEach(() => {
  cleanup()
  resetFeatureFlags()
})

describe('lifecycle off', () => {
  it('keeps the not-sent sentence and offers no Sign', async () => {
    orders = [{ ...NURSING, id: 'O-PLAIN', order_type: 'medication', description: 'Aspirin 75 mg' }]
    render(<OrdersPanel patientId="P-SURESH" />)
    await screen.findByText('Aspirin 75 mg')
    expect(screen.getByText(/^Not sent to the ward or pharmacy/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Sign/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Awaiting my signature/ })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '+ New Order' }))
    fireEvent.change(screen.getByDisplayValue('lab'), { target: { value: 'medication' } })
    expect(screen.getByRole('note').textContent).toMatch(/^Not sent to the ward or pharmacy/)
    expect(screen.queryByLabelText('Medicine')).toBeNull()
    expect(screen.getByPlaceholderText('Order description')).toBeTruthy()
  })

  it('says signing is switched off for a medicine linked before the switch went off', async () => {
    orders = [linkedOrder()]
    render(<OrdersPanel patientId="P-SURESH" />)
    await screen.findByText('Ceftriaxone 1 g IV once daily')
    expect(screen.getByText(/Signing is switched off on this computer/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Sign/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /→ active/ })).toBeNull()
  })
})

describe('lifecycle on', () => {
  beforeEach(() => lifecycle(true))

  it('asks for the medicine as fields and places it with them', async () => {
    render(<OrdersPanel patientId="P-SURESH" />)
    fireEvent.click(await screen.findByRole('button', { name: '+ New Order' }))
    fireEvent.change(screen.getByDisplayValue('lab'), { target: { value: 'medication' } })

    expect(screen.getByRole('note').textContent).toBe(
      'Reaches the ward chart and the drug round once a prescriber signs it under Orders. It is not sent to the pharmacy.')
    expect(screen.queryByPlaceholderText('Order description')).toBeNull()
    expect(screen.queryByLabelText('Code')).toBeNull()
    const create = screen.getByRole('button', { name: 'Create Order' }) as HTMLButtonElement
    expect(create.disabled).toBe(true)

    fireEvent.change(screen.getByLabelText('Medicine'), { target: { value: 'Ondansetron' } })
    fireEvent.change(screen.getByLabelText('Dose'), { target: { value: '4 mg' } })
    fireEvent.change(screen.getByLabelText('Route'), { target: { value: 'IV' } })
    expect(create.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('How often'), { target: { value: '8 hourly' } })
    fireEvent.click(screen.getByLabelText('Give only when needed'))
    fireEvent.change(screen.getByLabelText('Give when'), { target: { value: 'nausea' } })
    expect(create.disabled).toBe(false)
    fireEvent.click(create)

    await waitFor(() => expect(createCpoeMedicationOrder).toHaveBeenCalledTimes(1))
    expect(createCpoeMedicationOrder.mock.calls[0][0]).toEqual({
      patient_id: 'P-SURESH',
      description: 'Ondansetron 4 mg IV 8 hourly, as needed for nausea',
      priority: 'routine',
      drug_name: 'Ondansetron', dose: '4 mg', route: 'IV', frequency: '8 hourly',
      is_prn: true, prn_indication: 'nausea',
    })
  })

  it('gives a prescriber Sign on a draft, and signs the version on screen', async () => {
    orders = [linkedOrder(), NURSING]
    render(<OrdersPanel patientId="P-SURESH" />)
    await screen.findByText('Ceftriaxone 1 g IV once daily')
    expect(screen.getByText(/Waiting for a prescriber to sign it\. Not on the ward chart yet\./)).toBeTruthy()
    expect(screen.queryByText(/^Not sent to the ward or pharmacy/)).toBeNull()
    // The manual state machine is not offered for it, only for the nursing order.
    expect(screen.getAllByRole('button', { name: /→ active/ })).toHaveLength(1)

    fireEvent.click(screen.getByRole('button', { name: 'Sign Ceftriaxone 1 g IV once daily' }))
    await waitFor(() => expect(signMedOrder).toHaveBeenCalledTimes(1))
    expect(signMedOrder.mock.calls[0].slice(0, 2)).toEqual(['MO-1', 1])
    expect(signMedOrder.mock.calls[0][2]).toBeUndefined()
  })

  it('gives a nurse no Sign and no signature filter', async () => {
    role = 'nurse'
    orders = [linkedOrder()]
    render(<OrdersPanel patientId="P-SURESH" />)
    await screen.findByText('Ceftriaxone 1 g IV once daily')
    expect(screen.getByText(/Waiting for a prescriber to sign it/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Sign/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Awaiting my signature/ })).toBeNull()
  })

  it('filters to what awaits a signature, oldest first, and marks a long wait', async () => {
    orders = [
      linkedOrder({ id: 'O-NEW', description: 'Pantoprazole 40 mg oral once daily' },
        { id: 'MO-NEW', drug_name: 'Pantoprazole', created_at: '2026-10-09T09:00:00.000000+00:00' }),
      linkedOrder({ id: 'O-OLD', description: 'Enoxaparin 40 mg SC once daily' },
        { id: 'MO-OLD', drug_name: 'Enoxaparin', created_at: '2026-10-07T09:00:00.000000+00:00',
          waiting_over_24h: true }),
      linkedOrder({ id: 'O-SIGNED', description: 'Aspirin 75 mg oral once daily', status: 'active' },
        { id: 'MO-SIGNED', status: 'prescribed', version: 2, prescriber_name: 'Dr Rao' }),
      NURSING,
    ]
    render(<OrdersPanel patientId="P-SURESH" />)
    const toggle = await screen.findByRole('button', { name: 'Awaiting my signature (2)' })
    expect(toggle.getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(toggle)
    expect(screen.getByRole('button', { name: 'Show all orders' }).getAttribute('aria-pressed')).toBe('true')
    const titles = screen.getAllByRole('button', { name: /^Sign / }).map(b => b.getAttribute('aria-label'))
    expect(titles).toEqual(['Sign Enoxaparin 40 mg SC once daily', 'Sign Pantoprazole 40 mg oral once daily'])
    expect(screen.queryByText('Hourly observations')).toBeNull()
    expect(screen.queryByText('Aspirin 75 mg oral once daily')).toBeNull()
    expect(screen.getByText(/Waiting over 24 hours for a prescriber to sign it/)).toBeTruthy()
  })

  it('shows a signed medicine on the ward chart with no Sign and no Cancel', async () => {
    orders = [linkedOrder({ status: 'active' },
      { status: 'prescribed', version: 2, prescriber_name: 'Dr Rao' })]
    render(<OrdersPanel patientId="P-SURESH" />)
    await screen.findByText('Ceftriaxone 1 g IV once daily')
    expect(screen.getByText(/Signed by Dr Rao\. On the ward chart and the drug round\. Not sent to the pharmacy\./)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /^Sign/ })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull()
    expect(screen.queryByRole('button', { name: /→/ })).toBeNull()
  })

  it('needs a reason to cancel an unsigned medicine, and sends it', async () => {
    orders = [linkedOrder()]
    render(<OrdersPanel patientId="P-SURESH" />)
    await screen.findByText('Ceftriaxone 1 g IV once daily')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    const confirm = screen.getByRole('button', { name: 'Confirm cancel' }) as HTMLButtonElement
    expect(confirm.disabled).toBe(true)
    expect(screen.getByPlaceholderText('Reason (required)')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Cancellation reason for Ceftriaxone 1 g IV once daily'),
      { target: { value: 'Duplicate of the chart order' } })
    fireEvent.click(confirm)
    await waitFor(() => expect(apiFetch.mock.calls.some(c => c[0] === '/orders/O-MED/transition')).toBe(true))
    const call = apiFetch.mock.calls.find(c => c[0] === '/orders/O-MED/transition')!
    expect(JSON.parse(String(call[1]?.body))).toEqual({ to_status: 'cancelled', detail: 'Duplicate of the chart order' })
  })

  it('opens the acknowledgement on a 428 at sign and re-signs with it', async () => {
    orders = [linkedOrder()]
    const ackError = Object.assign(new Error('Drug safety acknowledgement required'), {
      code: 'safety_ack_required', status: 428,
      safety: {
        drug: 'Ceftriaxone', max_severity: 'severe', blocking: true, interaction_screened: true,
        allergy_alerts: [{ severity: 'severe', message: 'Patient is allergic to ceftriaxone.' }],
        interaction_alerts: [],
      },
    })
    signMedOrder.mockRejectedValueOnce(ackError)
    render(<OrdersPanel patientId="P-SURESH" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Sign Ceftriaxone 1 g IV once daily' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/Patient is allergic to ceftriaxone/)).toBeTruthy()
    fireEvent.change(within(dialog).getByRole('textbox'),
      { target: { value: 'Tolerated ceftriaxone last admission, discussed' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Acknowledge & Order' }))
    await waitFor(() => expect(signMedOrder).toHaveBeenCalledTimes(2))
    expect(signMedOrder.mock.calls[1]).toEqual(['MO-1', 1,
      { acknowledged: true, reason: 'Tolerated ceftriaxone last admission, discussed' }])
  })

  it('says when the pharmacy does not supply the medicine', async () => {
    orders = [linkedOrder({}, { on_formulary: false })]
    render(<OrdersPanel patientId="P-SURESH" />)
    await screen.findByText('Ceftriaxone 1 g IV once daily')
    expect(screen.getByText('Not supplied by pharmacy: this medicine is not on the formulary.')).toBeTruthy()
  })

  it('shows the server sentence when a signature is refused', async () => {
    orders = [linkedOrder()]
    signMedOrder.mockRejectedValueOnce(Object.assign(
      new Error('Enter the dose, the route and how often it is given before the order is signed'),
      { status: 422, detail: { error: 'dose_route_and_frequency_required' } }))
    render(<OrdersPanel patientId="P-SURESH" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Sign Ceftriaxone 1 g IV once daily' }))
    expect((await screen.findByRole('alert')).textContent).toMatch(
      /Ceftriaxone 1 g IV once daily: Enter the dose, the route and how often/)
  })
})
