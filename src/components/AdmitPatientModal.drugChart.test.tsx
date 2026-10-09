/**
 * The admission form on an Orb Ward Record computer (Samuel's ruling, 9
 * October 2026: hide the drug lists). Its History step asks for no current
 * medicines there (the server keeps none from an older screen either); the
 * allergy field stays. With the profile off it is as before.
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { resetClinicalSwitches, setClinicalSwitches } from '../shellConfig'

vi.mock('../api/client', () => ({
  admitPatient: vi.fn(),
  apiFetch: () => Promise.resolve({ departments: [], default_department: null, doctors: [],
                                    wards: [], beds: [] }),
}))
vi.mock('../App', () => ({
  useAuth: () => ({ user: { id: 'D001', name: 'Dr. Test', role: 'doctor', department: 'General Medicine' } }),
}))

import AdmitPatientModal from './AdmitPatientModal'

afterEach(() => { cleanup(); resetClinicalSwitches() })

async function openHistory() {
  render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /^History/ })) })
  expect(screen.getByText('Allergies')).toBeTruthy()
}

describe('the admission form and the drug chart', () => {
  it('asks for no current medicines on a Ward Record computer, and keeps the allergies', async () => {
    act(() => { setClinicalSwitches({ ward_record: true, scoring_enabled: false, models_enabled: false }) })
    await openHistory()
    expect(screen.queryByText('Current Medications')).toBeNull()
    expect(document.body.innerHTML).not.toMatch(/medication/i)
  })

  it('asks for the current medicines with the profile off', async () => {
    await openHistory()
    expect(screen.getByText('Current Medications')).toBeTruthy()
  })
})
