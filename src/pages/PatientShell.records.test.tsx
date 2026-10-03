/**
 * Patient portal: Lab History, Report History and follow-up requests (F079).
 *
 * Lab History was built from imaging alone, so a patient whose lab results were
 * charted read "No lab or imaging reports yet", while Report History listed the
 * same results as "lab_result". Results now come under `labs`, and every
 * document is named in plain words (`type_label`).
 *
 * A follow-up was posted to the staff booking route, saved as a booking the
 * care team had made, and the card said "Confirmed". It now goes to the
 * patient's request route and the card says the request was sent and that
 * nobody has agreed the time.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'

const reports = vi.fn()
const apiFetch = vi.fn()
vi.mock('../App', () => ({
  useAuth: () => ({ user: { id: 'P001', name: 'Raj Patel', role: 'patient' }, logout: vi.fn() }),
}))
vi.mock('../api/client', () => ({
  getBridgeStatus: () => Promise.resolve({ patient_name: 'Raj Patel', status: 'stable', status_message: 'Monitoring.' }),
  getBridgeReports: () => reports(),
  askBridge: vi.fn(),
  apiFetch: (...args: unknown[]) => apiFetch(...args),
}))
vi.mock('../components/bridge/CareTeamCard', () => ({ CareTeamCard: () => null }))
vi.mock('../components/bridge/VitalsTrendsCard', () => ({ VitalsTrendsCard: () => null }))

import PatientShell from './PatientShell'

const REPORTS = {
  patient_id: 'PAT1',
  medications: [],
  clinical_documents: [
    { doc_type: 'Ward Round', type_label: 'Ward round note', title: 'Manual Entry', content: 'Seen.', created_at: '2026-10-01 09:00:00' },
  ],
  labs: [
    { doc_type: 'lab_result', type_label: 'Lab result', title: 'Complete Blood Count', content: 'Hb 11.2 g/dL', created_at: '2026-10-01 06:00:00' },
  ],
  imaging_reports: [],
  discharge_summary: null,
}

let upcoming: unknown[] = []

beforeEach(() => {
  reports.mockReset()
  apiFetch.mockReset()
  upcoming = []
  reports.mockResolvedValue(REPORTS)
  apiFetch.mockImplementation((path: string) => {
    if (path === '/appointments/mine') return Promise.resolve({ appointments: upcoming })
    if (path === '/bridge/appointments') {
      return Promise.resolve({
        id: 'A1', status: 'booked', requested_date: '2026-10-06', requested_time: '10:00',
        department: 'Cardiology', requested_by_patient: true, time_chosen_by_patient: true,
        message: 'Your request is on the care team\'s appointment list.',
      })
    }
    return Promise.resolve({})
  })
})

async function renderShell() {
  const view = render(<PatientShell />)
  await waitFor(() => expect(reports).toHaveBeenCalled())
  return view
}

describe('Lab History', () => {
  it('lists lab results that arrive under labs, with no internal type on screen', async () => {
    const { container } = await renderShell()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Lab History/ })) })
    expect(await screen.findByText('Complete Blood Count')).toBeInTheDocument()
    expect(screen.queryByText('No lab or imaging reports yet')).toBeNull()
    expect(screen.getByText(/^Lab result · /)).toBeInTheDocument()
    expect(container.innerHTML).not.toContain('lab_result')

    await act(async () => { fireEvent.click(screen.getByText('Complete Blood Count')) })
    expect(screen.getByText('Hb 11.2 g/dL')).toBeInTheDocument()
    expect(screen.getByText(/^Type: Lab result · /)).toBeInTheDocument()
  })

  it('puts lab results and imaging in one list, newest first', async () => {
    reports.mockResolvedValue({
      ...REPORTS,
      imaging_reports: [
        { image_type: 'xray', clinical_context: 'Chest', analysis: 'Clear.', created_at: '2026-10-02T07:00:00+00:00' },
      ],
    })
    await renderShell()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Lab History/ })) })
    const names = Array.from(document.querySelectorAll('.bridge__report-item-name')).map(n => n.textContent)
    expect(names).toEqual(['XRAY: Chest', 'Complete Blood Count'])
  })

  it('says there are none only when there are neither labs nor imaging', async () => {
    reports.mockResolvedValue({ ...REPORTS, labs: [] })
    await renderShell()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Lab History/ })) })
    expect(screen.getByText('No lab or imaging reports yet')).toBeInTheDocument()
  })
})

describe('Report History', () => {
  it('names each document type in plain words', async () => {
    await renderShell()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Report History/ })) })
    expect(await screen.findByText(/^Ward round note · /)).toBeInTheDocument()
    expect(screen.queryByText(/Ward Round ·/)).toBeNull()
  })
})

describe('Schedule Follow-Up', () => {
  async function chooseSlot() {
    // Not today: today's times close as the day goes on, so 10:00 AM would only
    // be open before ten (PatientShell.schedule.test.tsx covers that).
    const openDays = () => Array.from(document.querySelectorAll<HTMLButtonElement>('.appt__week-cell'))
      .filter(b => !b.disabled && !b.classList.contains('appt__week-cell--today'))
    let open = openDays()
    if (open.length === 0) {
      fireEvent.click(screen.getByRole('button', { name: 'Next week' }))
      open = openDays()
    }
    fireEvent.click(open[0])
    fireEvent.click(screen.getByRole('button', { name: '10:00 AM' }))
    fireEvent.click(screen.getByRole('button', { name: /Cardio/ }))
    fireEvent.change(screen.getByPlaceholderText(/discuss my medication/), { target: { value: 'My tablets' } })
  }

  it('sends a request on the patient route and never says Confirmed', async () => {
    await renderShell()
    await chooseSlot()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Send Request/ })) })

    const call = apiFetch.mock.calls.find(([path]) => path === '/bridge/appointments')
    expect(call).toBeDefined()
    expect(call![1].method).toBe('POST')
    expect(JSON.parse(call![1].body)).toMatchObject({
      requested_time: '10:00', department: 'Cardiology', reason: 'My tablets',
    })
    expect(apiFetch.mock.calls.some(([path, init]) => path === '/appointments' && init?.method === 'POST')).toBe(false)

    expect(screen.getByText('Request sent')).toBeInTheDocument()
    expect(screen.getByText(/Nobody has agreed this time with you yet/)).toBeInTheDocument()
    expect(screen.queryByText('Confirmed')).toBeNull()
  })

  it('a slot the team had already booked is called a booking, not a request', async () => {
    apiFetch.mockImplementation((path: string) => {
      if (path === '/bridge/appointments') {
        return Promise.resolve({
          id: 'A2', status: 'booked', requested_date: '2026-10-06', requested_time: '10:00',
          department: null, requested_by_patient: false,
        })
      }
      return Promise.resolve({ appointments: [] })
    })
    await renderShell()
    await chooseSlot()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Send Request/ })) })
    expect(screen.getByText('Already booked')).toBeInTheDocument()
    expect(screen.queryByText('Request sent')).toBeNull()
  })

  it('an upcoming request the patient made is said to be one', async () => {
    upcoming = [
      { id: 'A1', scheduled_date: '2099-01-05', scheduled_time: '10:00', appointment_type: 'Patient request: confirm time', origin: 'patient', department: 'Cardiology' },
      { id: 'A2', scheduled_date: '2099-01-06', scheduled_time: '11:00', appointment_type: 'Follow-up', origin: 'staff', department: null },
    ]
    await renderShell()
    expect(await screen.findByText('Requested by you. The time is not agreed yet.')).toBeInTheDocument()
    expect(screen.getByText('Cardiology')).toBeInTheDocument()
    expect(screen.getByText('Follow-up')).toBeInTheDocument()
    expect(screen.queryByText('Patient request: confirm time')).toBeNull()
  })
})
