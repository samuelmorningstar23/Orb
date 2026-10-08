/**
 * The ward clerk's Observations screen (Orb Ward Record, 9 October 2026):
 * her list, its refusals in words, and the sheet it opens.
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const getTranscribePatients = vi.fn()
const recordObservations = vi.fn()
const getOwnCopies = vi.fn()
const voidObservation = vi.fn()

vi.mock('../../api/nurse', () => ({
  getTranscribePatients: () => getTranscribePatients(),
  recordObservations: (...args: unknown[]) => recordObservations(...args),
  previewNews2: vi.fn(),
  getOwnCopies: (...args: unknown[]) => getOwnCopies(...args),
  voidObservation: (...args: unknown[]) => voidObservation(...args),
}))

import WardObservationsPanel from './WardObservationsPanel'

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('orb_user', JSON.stringify({ id: 'WC1', name: 'Clerk', role: 'ward_clerk' }))
  getTranscribePatients.mockReset()
  recordObservations.mockReset()
  getOwnCopies.mockReset()
  voidObservation.mockReset()
})

afterEach(() => cleanup())

describe('the ward clerk\'s Observations screen', () => {
  it('lists her patients with ward, bed and hospital number, and opens the sheet for one', async () => {
    getTranscribePatients.mockResolvedValue({
      department: 'General Medicine',
      patients: [{ patient_id: 'P1', name: 'Sita Devi', mrn: 'UHID-1', ward: 'GM', bed: '4',
        admitted_at: '2026-10-07T08:50:00+00:00' }],
    })
    render(<WardObservationsPanel />)
    expect(await screen.findByText('Sita Devi')).toBeTruthy()
    expect(screen.getByText(/GM · Bed 4 · UHID-1 · Admitted/)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Copy from sheet for Sita Devi' }))
    expect(screen.getByRole('heading', { name: 'Copy observations from the sheet' })).toBeTruthy()
  })

  it('tells her when a copy older than 2 hours was charted without an alert', async () => {
    getTranscribePatients.mockResolvedValue({
      department: 'General Medicine',
      patients: [{ patient_id: 'P1', name: 'Sita Devi', mrn: 'UHID-1', ward: 'GM', bed: '4',
        admitted_at: '2026-10-01T08:50:00+00:00' }],
    })
    recordObservations.mockResolvedValue({
      vitals_id: 7, recorded_at: 'x', news2: null, schedule: null, replayed: false,
      live_hooks: false, message: 'Charted. Older than 2 hours, so no alert was raised.',
    })
    render(<WardObservationsPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Copy from sheet for Sita Devi' }))
    const when = new Date(Date.now() - 3 * 3600 * 1000)
    when.setSeconds(0, 0)
    const pad = (n: number) => String(n).padStart(2, '0')
    const local = `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}T${pad(when.getHours())}:${pad(when.getMinutes())}`
    fireEvent.change(screen.getByLabelText('Time on the sheet'), { target: { value: local } })
    fireEvent.change(document.getElementById('obs-heart_rate') as HTMLInputElement, { target: { value: '82' } })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Record observations' }))
    })
    expect(recordObservations).toHaveBeenCalledTimes(1)
    expect(await screen.findByText(
      'Observations saved for Sita Devi. Older than 2 hours, so no alert was raised.')).toBeTruthy()
  })

  it('says only that the set was saved when the server says nothing more', async () => {
    getTranscribePatients.mockResolvedValue({
      department: 'General Medicine',
      patients: [{ patient_id: 'P1', name: 'Sita Devi', mrn: 'UHID-1', ward: 'GM', bed: '4',
        admitted_at: null }],
    })
    recordObservations.mockResolvedValue({
      vitals_id: 8, recorded_at: 'x', news2: null, schedule: null, replayed: false,
    })
    render(<WardObservationsPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Copy from sheet for Sita Devi' }))
    const when = new Date(Date.now() - 30 * 60 * 1000)
    when.setSeconds(0, 0)
    const pad = (n: number) => String(n).padStart(2, '0')
    const local = `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}T${pad(when.getHours())}:${pad(when.getMinutes())}`
    fireEvent.change(screen.getByLabelText('Time on the sheet'), { target: { value: local } })
    fireEvent.change(document.getElementById('obs-heart_rate') as HTMLInputElement, { target: { value: '82' } })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Record observations' }))
    })
    expect(await screen.findByText('Observations saved for Sita Devi.')).toBeTruthy()
  })

  it('says so when no patient is admitted to her ward', async () => {
    getTranscribePatients.mockResolvedValue({ department: 'General Medicine', patients: [] })
    render(<WardObservationsPanel />)
    expect(await screen.findByText('No patients are admitted to your ward.')).toBeTruthy()
  })

  it('says in words when her role cannot see the list, and offers to try again', async () => {
    getTranscribePatients.mockRejectedValueOnce(Object.assign(new Error('Insufficient permissions'), { status: 403 }))
    render(<WardObservationsPanel />)
    expect(await screen.findByText('Your role cannot see this list.')).toBeTruthy()
    getTranscribePatients.mockResolvedValue({ department: null, patients: [] })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Try again' })) })
    expect(await screen.findByText('No patients are admitted to your ward.')).toBeTruthy()
  })

  it('says the ward list could not be reached when there is no answer', async () => {
    getTranscribePatients.mockRejectedValue(new TypeError('Failed to fetch'))
    render(<WardObservationsPanel />)
    expect(await screen.findByText('Could not reach the ward list. Check the connection and try again.'))
      .toBeTruthy()
  })
})

describe('her own copies', () => {
  const PATIENT = { patient_id: 'P1', name: 'Sita Devi', mrn: 'UHID-1', ward: 'GM', bed: '4', admitted_at: null }
  const READINGS = {
    heart_rate: 182, systolic_bp: 126, diastolic_bp: 78, respiratory_rate: 16,
    temperature: 36.9, spo2: 97, consciousness: 'alert', on_oxygen: false,
  }

  it('lists her copies for the patient and offers Entered in error only on a copy not yet struck out', async () => {
    getTranscribePatients.mockResolvedValue({ department: 'General Medicine', patients: [PATIENT] })
    getOwnCopies.mockResolvedValue({
      patient_id: 'P1',
      copies: [
        { vitals_id: 21, taken_at: '2026-10-08T22:50:00+00:00', readings: READINGS, voided: false },
        { vitals_id: 20, taken_at: '2026-10-08T18:50:00+00:00',
          readings: { ...READINGS, spo2: null, on_oxygen: null }, voided: true },
      ],
    })
    voidObservation.mockResolvedValue({ vitals_id: 21 })
    render(<WardObservationsPanel />)
    const yours = await screen.findByRole('button', { name: 'Your copies for Sita Devi' })
    await act(async () => { fireEvent.click(yours) })
    expect(getOwnCopies).toHaveBeenCalledWith('P1')
    const list = screen.getByRole('region', { name: 'Your copies for Sita Devi, last 24 hours' })
    expect(list.textContent).toContain('Pulse 182 · BP 126/78 · Breathing 16 · Temp 36.9 · SpO2 97 · Alert · On air')
    expect(list.textContent).toContain('SpO2 blank')
    expect(list.textContent).toContain('Air or oxygen blank')
    const voidButtons = screen.getAllByRole('button', { name: /entered in error/i })
    expect(voidButtons).toHaveLength(1)

    fireEvent.click(voidButtons[0])
    fireEvent.change(screen.getByLabelText('Why is this set wrong?'), { target: { value: 'Pulse typed as 182, sheet says 82' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Mark entered in error' })) })
    expect(voidObservation).toHaveBeenCalledWith(21, 'Pulse typed as 182, sheet says 82')
    expect(getOwnCopies).toHaveBeenCalledTimes(2)
  })

  it('says so when she has copied nothing for the patient today', async () => {
    getTranscribePatients.mockResolvedValue({ department: 'General Medicine', patients: [PATIENT] })
    getOwnCopies.mockResolvedValue({ patient_id: 'P1', copies: [] })
    render(<WardObservationsPanel />)
    const yours = await screen.findByRole('button', { name: 'Your copies for Sita Devi' })
    await act(async () => { fireEvent.click(yours) })
    expect(screen.getByText('You have not copied a set for Sita Devi in the last 24 hours.')).toBeTruthy()
  })
})
