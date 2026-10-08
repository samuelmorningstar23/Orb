/**
 * The ward clerk's Observations screen (Orb Ward Record, 9 October 2026):
 * her list, its refusals in words, and the sheet it opens.
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const getTranscribePatients = vi.fn()
const recordObservations = vi.fn()

vi.mock('../../api/nurse', () => ({
  getTranscribePatients: () => getTranscribePatients(),
  recordObservations: (...args: unknown[]) => recordObservations(...args),
  previewNews2: vi.fn(),
}))

import WardObservationsPanel from './WardObservationsPanel'

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('orb_user', JSON.stringify({ id: 'WC1', name: 'Clerk', role: 'ward_clerk' }))
  getTranscribePatients.mockReset()
  recordObservations.mockReset()
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
