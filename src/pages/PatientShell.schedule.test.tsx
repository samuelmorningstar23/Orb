/**
 * Patient portal follow-up picker (F144).
 *
 * The month label came from the week's Sunday alone, so the 27 Sep to 3 Oct
 * week read "Sep 2026" while the patient was choosing a day in October. Times
 * already over today were offered and the server refused them. Nothing said
 * what to do before a day was picked. The record download named a data format
 * patients do not know.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

const reports = vi.fn()
vi.mock('../App', () => ({
  useAuth: () => ({ user: { id: 'P001', name: 'Raj Patel', role: 'patient' }, logout: vi.fn() }),
}))
vi.mock('../api/client', () => ({
  getBridgeStatus: () => Promise.resolve({ patient_name: 'Raj Patel', status: 'stable', status_message: 'Monitoring.' }),
  getBridgeReports: () => reports(),
  askBridge: vi.fn(),
  apiFetch: () => Promise.resolve({ appointments: [] }),
}))
vi.mock('../components/bridge/CareTeamCard', () => ({ CareTeamCard: () => null }))
vi.mock('../components/bridge/VitalsTrendsCard', () => ({ VitalsTrendsCard: () => null }))

import PatientShell from './PatientShell'

async function renderAt(now: Date) {
  vi.setSystemTime(now)
  render(<PatientShell />)
  await waitFor(() => expect(reports).toHaveBeenCalled())
}

const monthLabel = () => document.querySelector('.appt__cal-month')?.textContent
const dayCell = (day: number) => Array.from(document.querySelectorAll<HTMLButtonElement>('.appt__week-cell'))
  .find(b => b.querySelector('.appt__week-num')?.textContent === String(day))!
const slot = (name: string) => screen.getByRole('button', { name }) as HTMLButtonElement

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  reports.mockReset()
  reports.mockResolvedValue({ patient_id: 'PAT1', medications: [], clinical_documents: [], labs: [], imaging_reports: [] })
})
afterEach(() => vi.useRealTimers())

describe('week label', () => {
  it('names both months for a week that crosses one', async () => {
    await renderAt(new Date(2026, 9, 2, 12, 0)) // Fri 2 Oct; week of Sun 27 Sep
    expect(monthLabel()).toBe('Sep to Oct 2026')
    fireEvent.click(screen.getByRole('button', { name: 'Next week' }))
    expect(monthLabel()).toBe('Oct 2026')
  })

  it('names both years for a week that crosses the new year', async () => {
    await renderAt(new Date(2026, 11, 30, 12, 0)) // Wed 30 Dec; week of Sun 27 Dec
    expect(monthLabel()).toBe('Dec 2026 to Jan 2027')
  })
})

describe('choosing a time', () => {
  it('says what to do before a day is picked', async () => {
    await renderAt(new Date(2026, 9, 2, 12, 0))
    expect(screen.getByText('Choose a day for your follow-up.')).toBeInTheDocument()
    fireEvent.click(dayCell(3))
    expect(screen.queryByText('Choose a day for your follow-up.')).toBeNull()
  })

  it('does not offer a time already over today', async () => {
    await renderAt(new Date(2026, 9, 2, 10, 30))
    fireEvent.click(dayCell(2))
    expect(slot('9:00 AM')).toBeDisabled()
    expect(slot('10:00 AM')).toBeDisabled()
    expect(slot('11:00 AM')).toBeEnabled()
    expect(slot('4:00 PM')).toBeEnabled()
    // A later day keeps every time.
    fireEvent.click(dayCell(3))
    expect(slot('9:00 AM')).toBeEnabled()
  })

  it('says so when every time today is over', async () => {
    await renderAt(new Date(2026, 9, 2, 19, 25))
    fireEvent.click(dayCell(2))
    expect(slot('4:00 PM')).toBeDisabled()
    expect(screen.getByText('No times are left today. Choose another day.')).toBeInTheDocument()
  })

  it('drops a chosen time that is already over on the day switched to', async () => {
    await renderAt(new Date(2026, 9, 2, 10, 30))
    fireEvent.click(dayCell(3))
    fireEvent.click(slot('9:00 AM'))
    expect(screen.getByText('Department')).toBeInTheDocument()
    fireEvent.click(dayCell(2))
    expect(slot('9:00 AM')).not.toHaveClass('appt__slot--active')
    expect(screen.queryByText('Department')).toBeNull()
  })
})

describe('record download', () => {
  it('is named in plain words', async () => {
    await renderAt(new Date(2026, 9, 2, 12, 0))
    expect(screen.getByRole('button', { name: 'Download my health record' })).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('FHIR')
  })
})

/* 2026-10-06: an appointment the care team booked shows its reason as they
   wrote it, under the type in plain words. */
describe('upcoming appointments', () => {
  it('shows the reason as written', async () => {
    vi.resetModules()
    vi.doMock('../api/client', () => ({
      getBridgeStatus: () => Promise.resolve({ patient_name: 'Raj Patel', status: 'stable', status_message: 'Monitoring.' }),
      getBridgeReports: () => reports(),
      askBridge: vi.fn(),
      apiFetch: () => Promise.resolve({ appointments: [{
        id: 'A1', patient_id: 'PAT1', booked_by_user_id: 'D1', scheduled_date: '2026-10-09',
        scheduled_time: '10:00', appointment_type: 'Post-discharge check',
        appointment_type_label: 'Post-discharge check', reason: 'Check the wound and take out the stitches',
        status: 'booked', created_at: '2026-10-02', origin: 'staff', department: null,
      }] }),
    }))
    const { default: Shell } = await import('./PatientShell')
    vi.setSystemTime(new Date(2026, 9, 2, 12, 0))
    render(<Shell />)
    expect(await screen.findByText('Check the wound and take out the stitches')).toBeInTheDocument()
    expect(screen.getByText('Post-discharge check')).toBeInTheDocument()
    vi.doUnmock('../api/client')
  })
})
