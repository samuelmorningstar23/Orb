/**
 * A severe allergy in the Verify Queue: only a prescriber can override it, as
 * at the bedside and at order time. A pharmacist gets no reason box, only the
 * plain sentence and Reject.
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

let signedIn: { id: string; name: string; role: string } | null = null
vi.mock('../../api/client', () => ({ getUser: () => signedIn }))

const ALLERGY = { type: 'allergy', severity: 'severe', message: 'Patient is allergic to amoxicillin (documented reaction: anaphylaxis).' }
const ROW = {
  id: 9, patient_id: 'P009', patient: 'Leela Das', drug_name: 'Amoxicillin',
  dose: '500mg', route: 'Oral', frequency: 'TDS', source: 'llm_extracted', status: 'active',
  safety_alerts: [ALLERGY], max_severity: 'severe', interaction_screened: true,
  coverage_note: null, screen_failed: false,
}
const verifyMedication = vi.fn()
vi.mock('../../api/pharmacy', () => ({
  getPendingVerifications: async () => [ROW],
  verifyMedication: (...args: unknown[]) => verifyMedication(...args),
  rejectMedication: vi.fn(),
}))

import PharmacyVerifyQueue from './PharmacyVerifyQueue'

beforeEach(() => {
  verifyMedication.mockReset()
  verifyMedication.mockResolvedValue({
    code: 'severe_ack_required',
    message: 'Severe drug-safety alerts were found for Amoxicillin. Give a reason to verify it anyway.',
    safety: { max_severity: 'severe', blocking: true, safe: false, alerts: [ALLERGY],
              override_requires_permission: 'medication.prescribe' },
  })
})

describe('Verify Queue severe allergy', () => {
  it('gives a pharmacist no reason box and says only a prescriber can override', async () => {
    signedIn = { id: 'PH1', name: 'Priya Nair', role: 'pharmacist' }
    render(<PharmacyVerifyQueue />)
    fireEvent.click(await screen.findByRole('button', { name: 'Verify' }))
    expect(await screen.findByText(/Only a prescriber can override an allergy block\./)).toBeInTheDocument()
    expect(screen.queryByLabelText(/Reason for verifying Amoxicillin/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Acknowledge & verify' })).toBeNull()
  })

  it('still offers the reason box to a prescriber', async () => {
    signedIn = { id: 'D1', name: 'Dr A', role: 'doctor' }
    render(<PharmacyVerifyQueue />)
    fireEvent.click(await screen.findByRole('button', { name: 'Verify' }))
    expect(await screen.findByLabelText(/Reason for verifying Amoxicillin/)).toBeInTheDocument()
  })
})
