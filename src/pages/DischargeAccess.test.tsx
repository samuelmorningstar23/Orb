/**
 * The patient's discharge link shows the drafted summary only once a clinician
 * has reviewed it, and names the hospital only from its configuration.
 */
import { render, screen, fireEvent, act } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it, vi, beforeEach } from 'vitest'

const verifyDischargeAccess = vi.fn<(...a: unknown[]) => Promise<{ access_grant: string }>>(async () => ({ access_grant: 'grant' }))
const getVerifiedDischargeSummary = vi.fn<(...a: unknown[]) => Promise<unknown>>()

vi.mock('../api/client', () => ({
  verifyDischargeAccess: (...a: unknown[]) => verifyDischargeAccess(...a),
  getVerifiedDischargeSummary: (...a: unknown[]) => getVerifiedDischargeSummary(...a),
}))

import DischargeAccess from './DischargeAccess'

const BASE = {
  status: 'success',
  timeline: [],
  patient: { mrn: 'MRN-0021', name: 'Ravi Kumar', age: 58, sex: 'M', admission_id: 'ADM-0021' },
  allergies: [{ substance: 'Penicillin', reaction: 'Rash', severity: 'moderate' }],
  comorbidities: [],
  medications: [{ name: 'Metformin', drug_name: 'Metformin', dosage: '500 mg', route: 'Oral', frequency: 'BD', status: 'active' }],
  unverified_medication_count: 0,
  site_name: null,
  discharged_at: '2026-10-03 01:00:00',
}

async function openLink() {
  render(
    <MemoryRouter initialEntries={['/discharge/tok']}>
      <Routes><Route path="/discharge/:token" element={<DischargeAccess />} /></Routes>
    </MemoryRouter>,
  )
  fireEvent.change(screen.getByLabelText('Date of Birth'), { target: { value: '1964-03-11' } })
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Access Records/ })) })
}

beforeEach(() => vi.clearAllMocks())

describe('the discharge link', () => {
  it('says the summary is being finalised while it is an unreviewed draft', async () => {
    getVerifiedDischargeSummary.mockResolvedValue({
      ...BASE, summary: null, summary_status: 'draft',
      summary_message: 'Your discharge summary is being finalised by your doctor.',
    })
    await openLink()
    expect(await screen.findByTestId('discharge-pending-review')).toHaveTextContent('being finalised')
    // The recorded facts still show; the drafted text does not exist here.
    expect(screen.getByText(/MRN-0021/)).toBeInTheDocument()
    expect(document.body.textContent).toContain('Penicillin')
    // No invented hospital name.
    expect(document.body.textContent).not.toContain('Orb Hospital Trust')
  })

  it('says a wrong date of birth does not match, with no attempt count', async () => {
    // What the server sends: no attempts_remaining, deliberately
    // (server/tests/test_discharge_access.py pins its absence).
    verifyDischargeAccess.mockRejectedValueOnce({ detail: { status: 'error', reason: 'invalid_dob' } })
    await openLink()
    expect(await screen.findByText('That date of birth does not match our records. Please check it and try again.')).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/undefined|attempts remaining/)
    expect(getVerifiedDischargeSummary).not.toHaveBeenCalled()
  })

  it('shows the summary once it has been reviewed', async () => {
    getVerifiedDischargeSummary.mockResolvedValue({
      ...BASE, summary: 'Admitted with pneumonia. Recovered.', summary_status: 'reviewed',
      reviewed_by_name: 'Dr Meera Raman', reviewed_at: '2026-10-03T02:00:00Z', site_name: 'City General Hospital',
    })
    await openLink()
    expect(await screen.findByText('Admitted with pneumonia. Recovered.')).toBeInTheDocument()
    expect(screen.queryByTestId('discharge-pending-review')).toBeNull()
    expect(screen.getAllByText('City General Hospital').length).toBeGreaterThan(0)
  })
})
