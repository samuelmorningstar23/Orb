/**
 * Set due times on the Orders screen (medorders-19, W8-I; ruling MO-R8).
 * Pinned here:
 *   * lifecycle on: a linked medicine given a fixed number of times a day says
 *     when it is due (the prescriber's times, the hospital's signed times, or
 *     that the round works them out); one with no daily count says nothing;
 *   * a prescriber gets "Set due times", prefilled from the hospital's times,
 *     and saving sends the times with the order's version; clearing sends an
 *     empty list; a refusal shows the server's words; Save waits for a valid
 *     time in every box;
 *   * a nurse sees the line but no button;
 *   * lifecycle off: nothing is read and nothing is shown.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const PATIENTS = [
  { id: 'P-SURESH', name: 'Suresh Reddy', mrn: 'MRN-GM004', bed: 'GM-104', ward: 'GM-1', age: 55, sex: 'M' },
]

let role = 'doctor'
let orders: unknown[] = []
let schedules: unknown = null
let putError: Error | null = null

const apiFetch = vi.fn(async (path: string, init?: RequestInit) => {
  if (/^\/orders\/patient\//.test(path)) return { orders }
  if (/^\/medorders\/patient\/.+\/schedules$/.test(path)) return schedules
  if (/^\/medorders\/orders\/.+\/schedule$/.test(path) && init?.method === 'PUT') {
    if (putError) throw putError
    return { order: { id: 'MO-1', status: 'prescribed', version: 3 }, schedule: null, replayed: false }
  }
  return { status: 'success' }
})

vi.mock('../../api/client', () => ({
  apiFetch: (path: string, init?: RequestInit) => apiFetch(path, init),
  createOrder: vi.fn(async () => ({ status: 'success', order: { id: 'O-NEW' } })),
  getPatients: async () => PATIENTS,
  isSafetyAckRequired: () => false,
  getUser: () => ({ role }),
  socket: { on: vi.fn(), off: vi.fn() },
}))

vi.mock('../../api/lab/orders', () => ({ cancelLabOrder: vi.fn() }))
vi.mock('../../api/lab/master', () => ({ searchLabCatalogue: vi.fn(async () => ({ tests: [] })) }))

import { resetFeatureFlags, setFeatureFlags } from '../../shellConfig'
import OrdersPanel from './OrdersPanel'

const DESCRIPTION = 'Amoxicillin 500 mg oral TDS'

function linkedOrder(med: Record<string, unknown> = {}) {
  return {
    id: 'O-MED', patient_id: 'P-SURESH', order_type: 'medication', code: null, code_system: null,
    description: DESCRIPTION, priority: 'routine', status: 'active', source: 'clinician',
    result_text: null, result_at: null, created_at: '2026-10-09 08:00:00',
    managed_by_medication_order: true,
    medication_order: {
      id: 'MO-1', status: 'prescribed', version: 2, source: 'cpoe', drug_name: 'Amoxicillin',
      dose: '500 mg', route: 'oral', frequency: 'TDS', is_prn: false, prn_indication: null,
      priority: 'routine', entered_by: 'D001', entered_by_role: 'doctor', entered_by_name: 'Dr Rao',
      prescriber_id: 'D001', prescriber_name: 'Dr Rao', prescribed_at: '2026-10-09T08:05:00+00:00',
      verified_by: null, verified_at: null, status_reason: null, status_changed_at: null,
      legacy_medication_id: 7, needs_verification: false,
      created_at: '2026-10-09T08:00:00.000000+00:00', waiting_over_24h: false, on_formulary: true,
      ...med,
    },
  }
}

function entry(over: Record<string, unknown> = {}) {
  return {
    patient_id: 'P-SURESH',
    site_times: { available: true, version: 'v1', fingerprint: 'sha256:x', signed: null, times: [] },
    orders: [{
      med_order_id: 'MO-1', status: 'prescribed', version: 2, frequency: 'TDS', doses_per_day: 3,
      schedule: null, site_times: ['08:00', '14:00', '20:00'], ...over,
    }],
  }
}

const SET_BY_PRESCRIBER = {
  times: ['06:00', '14:00', '22:00'], basis: 'prescriber', schedule_version: 1,
  site_times_version: null, site_times_fingerprint: null, authored_by: 'D001',
  authored_at: '2026-10-09T09:00:00+00:00',
}

const lifecycle = (on: boolean) => setFeatureFlags({ med_order_store: on, med_order_lifecycle: on })

beforeEach(() => {
  role = 'doctor'
  orders = [linkedOrder()]
  schedules = entry()
  putError = null
  apiFetch.mockClear()
  resetFeatureFlags()
})
afterEach(() => {
  cleanup()
  resetFeatureFlags()
})

describe('set due times, lifecycle on', () => {
  beforeEach(() => lifecycle(true))

  it("says the hospital's set times when none were set for the order", async () => {
    render(<OrdersPanel patientId="P-SURESH" />)
    expect(await screen.findByText("Due at the hospital's set times: 08:00, 14:00, 20:00."))
      .toBeTruthy()
  })

  it("says the prescriber's times when they were set", async () => {
    schedules = entry({ schedule: SET_BY_PRESCRIBER })
    render(<OrdersPanel patientId="P-SURESH" />)
    expect(await screen.findByText('Due at 06:00, 14:00, 22:00 (set by the prescriber).'))
      .toBeTruthy()
  })

  it('says the round works the times out when there are none', async () => {
    schedules = entry({ site_times: null })
    render(<OrdersPanel patientId="P-SURESH" />)
    expect(await screen.findByText(/^No set times: the drug round works the due times out/))
      .toBeTruthy()
  })

  it('says nothing for a medicine with no daily count', async () => {
    schedules = entry({ doses_per_day: null, site_times: null, frequency: 'PRN' })
    render(<OrdersPanel patientId="P-SURESH" />)
    await screen.findByText(DESCRIPTION)
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      '/medorders/patient/P-SURESH/schedules', undefined))
    expect(screen.queryByText(/^Due at|^No set times/)).toBeNull()
    expect(screen.queryByRole('button', { name: /Set due times/ })).toBeNull()
  })

  it("a prescriber starts from the hospital's times and saves with the version", async () => {
    render(<OrdersPanel patientId="P-SURESH" />)
    fireEvent.click(await screen.findByRole('button', { name: `Set due times for ${DESCRIPTION}` }))
    const group = screen.getByRole('group', { name: `Due times for ${DESCRIPTION}` })
    const boxes = within(group).getAllByPlaceholderText('HH:MM') as HTMLInputElement[]
    expect(boxes.map(b => b.value)).toEqual(['08:00', '14:00', '20:00'])
    fireEvent.change(boxes[0], { target: { value: '06:00' } })
    fireEvent.change(boxes[2], { target: { value: '22:00' } })
    fireEvent.click(within(group).getByRole('button', { name: 'Save times' }))
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      '/medorders/orders/MO-1/schedule',
      expect.objectContaining({ method: 'PUT' })))
    const call = apiFetch.mock.calls.find(c => c[0] === '/medorders/orders/MO-1/schedule')!
    expect(JSON.parse(String(call[1]?.body))).toEqual(
      { times: ['06:00', '14:00', '22:00'], expected_version: 2 })
    await waitFor(() =>
      expect(screen.queryByRole('group', { name: `Due times for ${DESCRIPTION}` })).toBeNull())
  })

  it('waits for a valid time in every box', async () => {
    schedules = entry({ site_times: null })
    render(<OrdersPanel patientId="P-SURESH" />)
    fireEvent.click(await screen.findByRole('button', { name: `Set due times for ${DESCRIPTION}` }))
    const group = screen.getByRole('group', { name: `Due times for ${DESCRIPTION}` })
    const save = within(group).getByRole('button', { name: 'Save times' }) as HTMLButtonElement
    expect(save.disabled).toBe(true)
    const boxes = within(group).getAllByPlaceholderText('HH:MM')
    fireEvent.change(boxes[0], { target: { value: '08:00' } })
    fireEvent.change(boxes[1], { target: { value: '14:00' } })
    fireEvent.change(boxes[2], { target: { value: '8pm' } })
    expect(save.disabled).toBe(true)
    fireEvent.change(boxes[2], { target: { value: '14:00' } })
    expect(save.disabled).toBe(true)  // the same time twice
    fireEvent.change(boxes[2], { target: { value: '20:00' } })
    expect(save.disabled).toBe(false)
  })

  it("shows the server's words when it refuses", async () => {
    putError = Object.assign(new Error('Two times are less than 60 minutes apart.'),
      { detail: { error: 'invalid_times' } })
    render(<OrdersPanel patientId="P-SURESH" />)
    fireEvent.click(await screen.findByRole('button', { name: `Set due times for ${DESCRIPTION}` }))
    fireEvent.click(screen.getByRole('button', { name: 'Save times' }))
    expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText('Two times are less than 60 minutes apart.')).toBeTruthy()
  })

  it('clears the set times with an empty list', async () => {
    schedules = entry({ schedule: SET_BY_PRESCRIBER })
    render(<OrdersPanel patientId="P-SURESH" />)
    fireEvent.click(await screen.findByRole('button', { name: `Set due times for ${DESCRIPTION}` }))
    fireEvent.click(screen.getByRole('button', { name: 'Clear set times' }))
    await waitFor(() => {
      const call = apiFetch.mock.calls.find(c => c[0] === '/medorders/orders/MO-1/schedule')
      expect(call && JSON.parse(String(call[1]?.body))).toEqual({ times: [], expected_version: 2 })
    })
  })

  it('a nurse sees the times but cannot set them', async () => {
    role = 'nurse'
    render(<OrdersPanel patientId="P-SURESH" />)
    expect(await screen.findByText("Due at the hospital's set times: 08:00, 14:00, 20:00."))
      .toBeTruthy()
    expect(screen.queryByRole('button', { name: /Set due times/ })).toBeNull()
  })
})

describe('set due times, lifecycle off', () => {
  it('reads nothing and shows nothing', async () => {
    render(<OrdersPanel patientId="P-SURESH" />)
    await screen.findByText(DESCRIPTION)
    expect(apiFetch.mock.calls.some(c => String(c[0]).includes('/schedules'))).toBe(false)
    expect(screen.queryByText(/^Due at|^No set times/)).toBeNull()
    expect(screen.queryByRole('button', { name: /Set due times/ })).toBeNull()
  })
})
