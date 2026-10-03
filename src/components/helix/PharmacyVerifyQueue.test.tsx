/**
 * Verify Queue: an AI-charted drug arrives with "REVIEW REQUIRED" placeholders.
 * Verify stays disabled until the pharmacist has entered a real dose, route and
 * frequency, so the placeholder can never become the verified order (the
 * server answers 422 for the same case).
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const verifyMedication = vi.fn()

const AI_ROW = {
  id: 7,
  patient_id: 'P001',
  patient: 'Asha Rao',
  drug_name: 'Enoxaparin',
  dose: 'REVIEW REQUIRED',
  route: 'Review',
  frequency: 'REVIEW REQUIRED',
  source: 'llm_extracted',
  status: 'active',
  safety_alerts: [],
  max_severity: 'none',
  interaction_screened: true,
  coverage_note: null as string | null,
  screen_failed: false,
}
const getPending = vi.fn(async () => [AI_ROW])

vi.mock('../../api/pharmacy', () => ({
  getPendingVerifications: () => getPending(),
  verifyMedication: (...args: unknown[]) => verifyMedication(...args),
  rejectMedication: vi.fn(),
}))

import PharmacyVerifyQueue from './PharmacyVerifyQueue'

describe('PharmacyVerifyQueue placeholder guard', () => {
  beforeEach(() => {
    verifyMedication.mockReset()
    getPending.mockImplementation(async () => [AI_ROW])
  })

  it('keeps Verify disabled until dose, route and frequency are all filled', async () => {
    render(<PharmacyVerifyQueue />)
    const verify = await screen.findByRole('button', { name: 'Verify' })
    const dose = screen.getByLabelText('Dose for Enoxaparin') as HTMLInputElement
    const route = screen.getByLabelText('Route for Enoxaparin') as HTMLInputElement
    const freq = screen.getByLabelText('Frequency for Enoxaparin') as HTMLInputElement

    // the placeholders are blanked, not offered as values
    expect(dose.value).toBe('')
    expect(route.value).toBe('')
    expect(freq.value).toBe('')
    expect(verify).toBeDisabled()
    expect(screen.getByText('Enter a dose, route and frequency before verifying')).toBeInTheDocument()

    fireEvent.change(dose, { target: { value: '40mg' } })
    fireEvent.change(route, { target: { value: 'SC' } })
    expect(verify).toBeDisabled()

    // typing the placeholder back in does not count
    fireEvent.change(freq, { target: { value: 'review required' } })
    expect(verify).toBeDisabled()
    fireEvent.click(verify)
    expect(verifyMedication).not.toHaveBeenCalled()

    fireEvent.change(freq, { target: { value: 'OD' } })
    expect(verify).toBeEnabled()
    expect(screen.queryByText('Enter a dose, route and frequency before verifying')).not.toBeInTheDocument()

    verifyMedication.mockResolvedValue({ status: 'verified' })
    fireEvent.click(verify)
    expect(verifyMedication).toHaveBeenCalledWith(
      7,
      { dosage: '40mg', route: 'SC', frequency: 'OD' },
      undefined,
    )
    await waitFor(() => expect(screen.getByText('Verified Enoxaparin for Asha Rao')).toBeInTheDocument())
  })

  it('never shows a partly screened drug as a clean "No alerts"', async () => {
    const note = 'Not screened against Obscuremab: outside the interaction knowledge base. Check those pairs manually.'
    getPending.mockImplementation(async () => [{ ...AI_ROW, coverage_note: note }])
    render(<PharmacyVerifyQueue />)
    expect(await screen.findByText('Partly screened')).toBeInTheDocument()
    expect(screen.getByText(note)).toBeInTheDocument()
    expect(screen.queryByText('No alerts')).not.toBeInTheDocument()
  })
})
