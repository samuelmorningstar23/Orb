/**
 * FrontDeskPanel — the four things this screen must not get wrong.
 *
 *  1. "NOT CONFIGURED" MUST NOT RENDER AS ₹0.00. An unpriced consultation is a
 *     charge waiting for a rate, not a free consultation. A clerk who reads
 *     "₹0.00" tells the patient there is nothing to pay.
 *
 *  2. AN UNMEASURED WAIT MUST NOT RENDER AS "0 min". `waiting_minutes: null`
 *     means the server could not compute it; printing zero says the patient just
 *     walked in when they may have been sitting there since eight.
 *
 *  3. THE TRANSFER CONFIRM MUST STATE THE HANDOVER BEFORE THE CLICK. A transfer
 *     changes who can read the chart — the receiving ward gains it and the
 *     sending ward loses it the moment it commits. That is a consequence a clerk
 *     has to be told, not one they should discover.
 *
 *  4. THE DESTINATION IS A CLOSED LIST. Only departments the server said have
 *     clinical cover may be chosen; a free-text department box is how a patient
 *     ends up admitted on a ward that appears on nobody's board.
 *
 * The panel is driven through a mocked `apiFetch`, so the REAL src/api/frontdesk.ts
 * (URL building, the null-vs-zero display helpers) is exercised rather than stubbed.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import FrontDeskPanel from './FrontDeskPanel'

const apiFetch = vi.fn()

vi.mock('../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
}))

const DEPT = 'Cardiology'

function visit(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: 'OPD-1',
    patient_id: 'PAT-1',
    patient_name: 'Anjali Rao',
    patient_mrn: 'MRN-0001',
    patient_age: 54,
    patient_sex: 'F',
    department: DEPT,
    doctor_id: 'DOC-1',
    doctor_name: 'Dr Iyer',
    visit_date: '2026-08-12',
    token_number: 1,
    visit_type: 'new',
    chief_complaint: 'chest pain',
    status: 'waiting',
    registered_at: '2026-08-12T03:00:00+00:00',
    called_at: null,
    completed_at: null,
    cancelled_at: null,
    cancel_reason: null,
    consultation_charge_id: null,
    notes: null,
    waiting_minutes: 12,
    consultation_minutes: null,
    ...overrides,
  }
}

function queueWith(visits: ReturnType<typeof visit>[], longestWait: number | null = 12) {
  return {
    visit_date: '2026-08-12',
    department: DEPT,
    queues: [{
      department: DEPT,
      doctor_id: 'DOC-1',
      doctor_name: 'Dr Iyer',
      visits,
      waiting_count: visits.filter(v => v.status === 'waiting').length,
      in_consultation_count: visits.filter(v => v.status === 'in_consultation').length,
      longest_wait_minutes: longestWait,
    }],
    total: visits.length,
  }
}

const WARD_PATIENTS = [
  { id: 'PAT-1', name: 'Anjali Rao', mrn: 'MRN-0001', ward: 'Cardio Bay 3', bed: 'C-4',
    admission_department: DEPT },
]

const DESTINATIONS = {
  destinations: [
    { department: 'Intensive Care', staff_count: 6, wards: [{ ward: 'ICU Bay 2', occupied: 3 }] },
    { department: 'Cardiology', staff_count: 4, wards: [{ ward: 'Cardio Bay 3', occupied: 5 }] },
  ],
}

/** The STAFF ROSTER, which is a different question from "who has a queue".
 *  Dr Bose is deliberately not in `queueWith` — she is the doctor an empty
 *  morning must still let the desk book. */
const ROSTER = {
  doctors: [
    { id: 'DOC-1', name: 'Dr Iyer', department: DEPT },
    { id: 'DOC-2', name: 'Dr Bose', department: DEPT },
  ],
}

/** An empty clinic day: no visits registered yet, so no doctor has a queue. */
function emptyQueue() {
  return { visit_date: '2026-08-12', department: DEPT, queues: [], total: 0 }
}

/** Route the mocked apiFetch by path, so each test only overrides what it cares
 *  about. Anything unrouted throws loudly rather than resolving to undefined. */
function routeApi(overrides: Record<string, unknown> = {}) {
  apiFetch.mockImplementation(async (path: string) => {
    for (const [prefix, value] of Object.entries(overrides)) {
      if (path.startsWith(prefix)) return typeof value === 'function' ? (value as () => unknown)() : value
    }
    if (path.startsWith('/frontdesk/opd/queue')) return queueWith([visit()])
    if (path.startsWith('/frontdesk/opd/doctors')) return ROSTER
    if (path.startsWith('/vigil/patients')) return WARD_PATIENTS
    if (path.startsWith('/frontdesk/transfers/destinations')) return DESTINATIONS
    if (path.startsWith('/frontdesk/patients/')) return { patient_id: 'PAT-1', movements: [] }
    throw new Error(`unrouted apiFetch path in test: ${path}`)
  })
}

beforeEach(() => {
  apiFetch.mockReset()
  routeApi()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('FrontDeskPanel — OPD queue', () => {
  it('renders the token queue in order with the patient and the wait', async () => {
    routeApi({
      '/frontdesk/opd/queue': queueWith([
        visit({ id: 'OPD-1', token_number: 1, waiting_minutes: 42 }),
        visit({ id: 'OPD-2', token_number: 2, patient_name: 'Vikram Shah',
                patient_mrn: 'MRN-0002', waiting_minutes: 8 }),
      ], 42),
    })
    render(<FrontDeskPanel />)

    const rows = await screen.findAllByRole('row')
    // header + two visits (the transfers table is not mounted on this view)
    const tokens = rows.slice(1).map(r => within(r).getAllByRole('cell')[0].textContent)
    expect(tokens).toEqual(['1', '2'])
    expect(screen.getByText('Anjali Rao')).toBeInTheDocument()
    expect(screen.getByText('Vikram Shah')).toBeInTheDocument()
    // Twice: the first patient's pill and the queue's "longest wait" stat.
    expect(screen.getAllByText('42 min')).toHaveLength(2)
    expect(screen.getByText('8 min')).toBeInTheDocument()
  })

  it('renders an UNMEASURED wait as unknown, never as zero minutes', async () => {
    routeApi({
      '/frontdesk/opd/queue': queueWith([visit({ waiting_minutes: null })], null),
    })
    render(<FrontDeskPanel />)

    // Both the per-patient pill and the queue's "longest wait" stat.
    const unknowns = await screen.findAllByText('Unknown')
    expect(unknowns.length).toBeGreaterThanOrEqual(2)
    // The defect this guards: `formatWait(minutes ?? 0)` would print this.
    expect(screen.queryByText('0 min')).toBeNull()
  })

  it('offers Call in for a waiting patient and Complete once they are in', async () => {
    routeApi({
      '/frontdesk/opd/queue': queueWith([
        visit({ id: 'OPD-1', token_number: 1, status: 'waiting' }),
        visit({ id: 'OPD-2', token_number: 2, status: 'in_consultation',
                called_at: '2026-08-12T03:20:00+00:00' }),
      ]),
    })
    render(<FrontDeskPanel />)

    expect(await screen.findByRole('button', { name: 'Call in' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Complete' })).toBeInTheDocument()
    expect(screen.getByText('In consultation')).toBeInTheDocument()
  })
})

/**
 * REGISTERING THE FIRST VISIT OF THE DAY.
 *
 * Both defects here were invisible until ADM001's ward scoping was corrected to
 * NULL, because until then every front-desk account was scoped and every test
 * day already had a queue on it.
 */
describe('FrontDeskPanel — registering a visit', () => {
  it('offers the whole roster on an EMPTY morning, not just doctors who already have a queue', async () => {
    routeApi({ '/frontdesk/opd/queue': emptyQueue() })
    render(<FrontDeskPanel />)

    const select = await screen.findByLabelText('Consulting doctor')
    const names = within(select).getAllByRole('option').map(o => o.textContent)
    // The defect: this list was derived from `queue.queues`, so an empty day
    // offered only "Unassigned" — the first booking of the day could never be
    // assigned to anybody, so no doctor ever got a queue.
    expect(names).toEqual(['Unassigned', 'Dr Iyer', 'Dr Bose'])
  })

  /* F151: this used to fold such a doctor back in from the queue. The server
     now refuses a booking with anyone who is not an active doctor of the
     clinic, so offering them would only end in a refusal. Their waiting
     patients still show in the queue. */
  it('does not offer a doctor who has left the roster, though their queue still shows', async () => {
    // Deactivated mid-clinic: gone from /opd/doctors, still holding a queue.
    routeApi({ '/frontdesk/opd/doctors': { doctors: [{ id: 'DOC-2', name: 'Dr Bose', department: DEPT }] } })
    render(<FrontDeskPanel />)

    const select = await screen.findByLabelText('Consulting doctor')
    const names = within(select).getAllByRole('option').map(o => o.textContent)
    expect(names).toEqual(['Unassigned', 'Dr Bose'])
    expect(screen.getByRole('heading', { name: 'Dr Iyer' })).toBeInTheDocument()
  })

  it('offers only the chosen clinic\'s doctors to a governance account', async () => {
    routeApi({
      '/frontdesk/opd/queue': { ...emptyQueue(), department: null },
      '/frontdesk/opd/doctors': { doctors: [
        { id: 'DOC-1', name: 'Dr Iyer', department: 'Cardiology' },
        { id: 'DOC-7', name: 'Dr Menon', department: 'Intensive Care' },
      ] },
    })
    render(<FrontDeskPanel />)

    const select = await screen.findByLabelText('Consulting doctor')
    // No clinic yet: no doctor can be right.
    expect(within(select).getAllByRole('option').map(o => o.textContent))
      .toEqual(['Choose a clinic first'])

    fireEvent.change(screen.getByLabelText('Clinic'), { target: { value: 'Intensive Care' } })
    expect(within(select).getAllByRole('option').map(o => o.textContent))
      .toEqual(['Unassigned', 'Dr Menon'])
  })

  it('tells two doctors of the same name apart', async () => {
    routeApi({
      '/frontdesk/opd/queue': emptyQueue(),
      '/frontdesk/opd/doctors': { doctors: [
        { id: 'DOC-1', name: 'Dr Rao', department: DEPT },
        { id: 'DOC-9', name: 'Dr Rao', department: DEPT },
      ] },
    })
    render(<FrontDeskPanel />)
    const select = await screen.findByLabelText('Consulting doctor')
    expect(within(select).getAllByRole('option').map(o => o.textContent))
      .toEqual(['Unassigned', 'Dr Rao (DOC-1)', 'Dr Rao (DOC-9)'])
  })

  it('does not ask a SCOPED desk which clinic — the server fills in their own', async () => {
    render(<FrontDeskPanel />)
    await screen.findByLabelText('Consulting doctor')
    expect(screen.queryByLabelText('Clinic')).toBeNull()

    fireEvent.change(screen.getByLabelText('Patient'), { target: { value: 'PAT-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Register & issue token' }))

    await waitFor(() => {
      const call = apiFetch.mock.calls.find(c => c[0] === '/frontdesk/opd/visits')
      expect(call).toBeTruthy()
      // Sending it back would only create a way to disagree with the server.
      expect(JSON.parse((call![1] as RequestInit).body as string).department).toBeUndefined()
    })
  })

  it('asks an UNSCOPED governance account which clinic, and sends it', async () => {
    // `department: null` is what the server answers for an account with no
    // department on record — the shape ADM001 has since its scoping was fixed.
    routeApi({ '/frontdesk/opd/queue': { ...emptyQueue(), department: null } })
    render(<FrontDeskPanel />)

    const clinic = await screen.findByLabelText('Clinic')
    // The legal clinics are the departments with clinical cover, not free text.
    expect(within(clinic).getAllByRole('option').map(o => o.textContent))
      .toEqual(['Select a clinic…', 'Intensive Care', 'Cardiology'])

    // The defect: with no way to name a clinic, the POST came back 422
    // "department is required for an account with no department on record", so
    // this account could not register a visit at all.
    const submit = screen.getByRole('button', { name: 'Register & issue token' })
    fireEvent.change(screen.getByLabelText('Patient'), { target: { value: 'PAT-1' } })
    expect(submit).toBeDisabled()

    fireEvent.change(clinic, { target: { value: 'Cardiology' } })
    expect(submit).toBeEnabled()
    fireEvent.click(submit)

    await waitFor(() => {
      const call = apiFetch.mock.calls.find(c => c[0] === '/frontdesk/opd/visits')
      expect(call).toBeTruthy()
      expect(JSON.parse((call![1] as RequestInit).body as string).department).toBe('Cardiology')
    })
  })
})

describe('FrontDeskPanel — the consultation charge', () => {
  it('shows a priced consultation as a rupee amount', async () => {
    routeApi({
      '/frontdesk/opd/queue': queueWith([visit({ status: 'in_consultation' })]),
      '/frontdesk/opd/visits/OPD-1/complete': () => ({
        ...visit({ status: 'completed' }),
        consultation_charge: {
          charge_id: 7,
          service_code: 'OPD-CONSULT',
          description: 'Cardiology consultation',
          rate_paise: 45000,
          rate_display: '₹450.00',
          rate_is_configured: true,
          total_paise: 45000,
          total_display: '₹450.00',
          gst_treatment: 'exempt',
          rate_source: 'service_default',
        },
      }),
    })
    render(<FrontDeskPanel />)

    fireEvent.click(await screen.findByRole('button', { name: 'Complete' }))
    expect(await screen.findByText('₹450.00')).toBeInTheDocument()
  })

  it('shows an UNPRICED consultation as "Not configured", never ₹0.00', async () => {
    routeApi({
      '/frontdesk/opd/queue': queueWith([visit({ status: 'in_consultation' })]),
      '/frontdesk/opd/visits/OPD-1/complete': () => ({
        ...visit({ status: 'completed' }),
        consultation_charge: {
          charge_id: 8,
          service_code: 'OPD-CONSULT',
          description: 'Cardiology consultation',
          rate_paise: null,
          rate_display: 'Not configured',
          rate_is_configured: false,
          total_paise: 0,
          total_display: 'Not configured',
          gst_treatment: 'exempt',
          rate_source: 'unconfigured',
        },
      }),
    })
    render(<FrontDeskPanel />)

    fireEvent.click(await screen.findByRole('button', { name: 'Complete' }))

    expect(await screen.findByText('Not configured')).toBeInTheDocument()
    // THE DEFECT: rendering total_paise (0) because "the number is right there".
    expect(screen.queryByText('₹0.00')).toBeNull()
    expect(screen.queryByText('₹0')).toBeNull()
    // And the screen says WHY, so nobody records it as a free consultation.
    expect(
      screen.getByText(/waiting for a rate/i),
    ).toBeInTheDocument()
  })
})

describe('FrontDeskPanel: the consultation charge after the last patient', () => {
  it('still shows the charge when completing the visit empties the queue', async () => {
    let completed = false
    routeApi({
      '/frontdesk/opd/queue': () => (completed ? emptyQueue() : queueWith([visit({ status: 'in_consultation' })])),
      '/frontdesk/opd/visits/OPD-1/complete': () => {
        completed = true
        return {
          ...visit({ status: 'completed' }),
          consultation_charge: {
            charge_id: 9, service_code: 'OPD-CONSULT', description: 'Cardiology consultation',
            rate_paise: 80000, rate_display: '₹800.00', rate_is_configured: true,
            total_paise: 80000, total_display: '₹800.00', gst_treatment: 'exempt',
            rate_source: 'service_default',
          },
        }
      },
    })
    render(<FrontDeskPanel />)

    fireEvent.click(await screen.findByRole('button', { name: 'Complete' }))
    // The empty state and the confirmation, together: the defect returned the
    // empty note first and the charge was never shown.
    expect(await screen.findByText(/No one is waiting/)).toBeInTheDocument()
    expect(screen.getByText('Consultation charge raised')).toBeInTheDocument()
    expect(screen.getByText('₹800.00')).toBeInTheDocument()
  })
})

describe('FrontDeskPanel — transfers', () => {
  async function openTransferModal() {
    render(<FrontDeskPanel />)
    fireEvent.click(await screen.findByRole('tab', { name: 'Transfers' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Transfer' }))
    return { dialog: await screen.findByRole('dialog') }
  }

  it('warns that the transfer hands the chart over BEFORE it is recorded', async () => {
    const { dialog } = await openTransferModal()

    // The warning is present at the moment of decision, alongside the still-
    // unclicked confirm button — not shown afterwards as an explanation.
    expect(within(dialog).getByText(/hands the chart over/i)).toBeInTheDocument()
    expect(
      within(dialog).getByText(/can no longer see them/i),
    ).toBeInTheDocument()
    expect(
      within(dialog).getByRole('button', { name: 'Record transfer' }),
    ).toBeInTheDocument()
    // Nothing has been written yet.
    expect(apiFetch).not.toHaveBeenCalledWith('/frontdesk/transfers', expect.anything())
  })

  it('portals the modal to document.body', async () => {
    const { dialog } = await openTransferModal()
    // A modal nested inside the panel inherits the panel's stacking/overflow
    // context and gets clipped by the scrolling column it lives in.
    expect(dialog.parentElement?.parentElement).toBe(document.body)
  })

  it('only offers departments the server said have clinical cover', async () => {
    const { dialog } = await openTransferModal()
    const select = within(dialog).getByLabelText('Receiving department') as HTMLSelectElement
    const options = [...select.options].map(o => o.value).filter(Boolean)
    expect(options).toEqual(['Intensive Care', 'Cardiology'])
    // There must be NO free-text way to name a department: an invented ward is
    // how a patient ends up admitted where nobody covers them.
    expect(select.tagName).toBe('SELECT')
  })

  it('will not submit without a destination and a reason', async () => {
    const { dialog } = await openTransferModal()
    const confirm = within(dialog).getByRole('button', { name: 'Record transfer' })
    expect(confirm).toBeDisabled()

    fireEvent.change(within(dialog).getByLabelText('Receiving department'), { target: { value: 'Intensive Care' } })
    expect(confirm).toBeDisabled()          // still no reason

    fireEvent.change(within(dialog).getByLabelText('Reason for transfer'), { target: { value: 'Needs ventilation' } })
    expect(confirm).toBeEnabled()
  })

  it('sends the transfer with its from/to and reason, then reloads', async () => {
    const { dialog } = await openTransferModal()
    fireEvent.change(within(dialog).getByLabelText('Receiving department'), { target: { value: 'Intensive Care' } })
    fireEvent.change(within(dialog).getByLabelText('Ward / bay'), { target: { value: 'ICU Bay 2' } })
    fireEvent.change(within(dialog).getByLabelText('Reason for transfer'), { target: { value: 'Needs ventilation' } })

    routeApi({
      '/frontdesk/transfers/destinations': DESTINATIONS,
      '/frontdesk/transfers': () => ({
        movement: {
          id: 'MOV-1', patient_id: 'PAT-1', admission_id: 'ADM-1',
          movement_type: 'transfer',
          from_department: DEPT, from_ward: 'Cardio Bay 3', from_bed: 'C-4',
          to_department: 'Intensive Care', to_ward: 'ICU Bay 2', to_bed: null,
          reason: 'Needs ventilation', moved_at: '2026-08-12T04:00:00+00:00',
          moved_by: 'DOC-1', moved_by_name: 'Dr Iyer', notes: null,
        },
        patient_id: 'PAT-1', patient_name: 'Anjali Rao', admission_id: 'ADM-1',
        admission_status: 'active', patient_status: 'admitted',
        current_department: 'Intensive Care', current_ward: 'ICU Bay 2',
        current_bed: 'C-4',
      }),
    })

    fireEvent.click(within(dialog).getByRole('button', { name: 'Record transfer' }))

    await waitFor(() => {
      const call = apiFetch.mock.calls.find(c => c[0] === '/frontdesk/transfers')
      expect(call).toBeTruthy()
      const body = JSON.parse((call![1] as RequestInit).body as string)
      expect(body.patient_id).toBe('PAT-1')
      expect(body.to_department).toBe('Intensive Care')
      expect(body.to_ward).toBe('ICU Bay 2')
      expect(body.reason).toBe('Needs ventilation')
      // The client NEVER sends a department for itself to be scoped by; the
      // only department it names is the DESTINATION.
      expect(body).not.toHaveProperty('department')
    })

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('keeps the modal open and shows the refusal when the server rejects a ward', async () => {
    const { dialog } = await openTransferModal()
    fireEvent.change(within(dialog).getByLabelText('Receiving department'), { target: { value: 'Intensive Care' } })
    fireEvent.change(within(dialog).getByLabelText('Reason for transfer'), { target: { value: 'Bed shuffle' } })

    routeApi({
      '/frontdesk/transfers/destinations': DESTINATIONS,
      '/frontdesk/transfers': () => {
        throw new Error("'Ward 7' is not a department with clinical cover.")
      },
    })

    fireEvent.click(within(dialog).getByRole('button', { name: 'Record transfer' }))

    // The dialog must SURVIVE the refusal with the typed reason intact — closing
    // it would read as "done" for a move that did not happen.
    expect(await screen.findByText(/not a department with clinical cover/i)).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByLabelText('Reason for transfer')).toHaveValue('Bed shuffle')
  })
})

describe('FrontDeskPanel — failure states', () => {
  it('a failed load reads as an error, never as an empty desk', async () => {
    apiFetch.mockRejectedValue(new Error('Network unreachable'))
    render(<FrontDeskPanel />)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Network unreachable')
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })
})
