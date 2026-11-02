/**
 * Verify Queue reject: the reason is asked for inline, beside the drug.
 *
 * It used to ask through window.prompt, a browser dialog with no drug name in
 * view that some kiosk browsers block outright (then nothing could be rejected).
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'

const rejectMedication = vi.fn()

const ROW = {
  id: 11,
  patient_id: 'P001',
  patient: 'Asha Rao',
  drug_name: 'Enoxaparin',
  dose: '40mg',
  route: 'SC',
  frequency: 'OD',
  source: 'agentic',
  status: 'active',
  safety_alerts: [],
  max_severity: 'none',
  interaction_screened: true,
  coverage_note: null as string | null,
  screen_failed: false,
}

vi.mock('../../api/pharmacy', () => ({
  getPendingVerifications: async () => [ROW],
  verifyMedication: vi.fn(),
  rejectMedication: (...args: unknown[]) => rejectMedication(...args),
}))

import PharmacyVerifyQueue from './PharmacyVerifyQueue'

describe('PharmacyVerifyQueue inline reject', () => {
  let promptSpy: MockInstance<typeof window.prompt>

  beforeEach(() => {
    rejectMedication.mockReset()
    promptSpy = vi.spyOn(window, 'prompt').mockImplementation(() => 'should never be used')
  })
  afterEach(() => { promptSpy.mockRestore() })

  it('asks for the reason in the row, never through a browser prompt', async () => {
    render(<PharmacyVerifyQueue />)
    const reject = await screen.findByRole('button', { name: 'Reject' })
    expect(reject).toHaveAttribute('aria-expanded', 'false')

    fireEvent.click(reject)
    expect(promptSpy).not.toHaveBeenCalled()
    expect(reject).toHaveAttribute('aria-expanded', 'true')

    const reason = screen.getByLabelText('Reason for rejecting Enoxaparin for Asha Rao')
    const confirm = screen.getByRole('button', { name: 'Confirm rejection' })
    // Nothing is sent without a reason, and whitespace is not a reason.
    expect(confirm).toBeDisabled()
    fireEvent.change(reason, { target: { value: '   ' } })
    expect(confirm).toBeDisabled()
    fireEvent.click(confirm)
    expect(rejectMedication).not.toHaveBeenCalled()

    rejectMedication.mockResolvedValue({ status: 'rejected' })
    fireEvent.change(reason, { target: { value: '  Duplicate anticoagulant  ' } })
    expect(confirm).toBeEnabled()
    fireEvent.click(confirm)
    expect(rejectMedication).toHaveBeenCalledWith(11, 'Duplicate anticoagulant')
    await waitFor(() => expect(screen.getByText('Rejected Enoxaparin for Asha Rao')).toBeInTheDocument())
    expect(screen.queryByText('Confirm rejection')).not.toBeInTheDocument()
  })

  it('Cancel closes the panel without rejecting', async () => {
    render(<PharmacyVerifyQueue />)
    fireEvent.click(await screen.findByRole('button', { name: 'Reject' }))
    fireEvent.change(screen.getByLabelText('Reason for rejecting Enoxaparin for Asha Rao'), { target: { value: 'Wrong patient' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByLabelText('Reason for rejecting Enoxaparin for Asha Rao')).not.toBeInTheDocument()
    expect(rejectMedication).not.toHaveBeenCalled()
    expect(screen.getByText('Enoxaparin')).toBeInTheDocument()
  })

  it('keeps the drug and the typed reason when the server refuses', async () => {
    rejectMedication.mockRejectedValue(new Error('Rejection failed: the medicine was already stopped'))
    render(<PharmacyVerifyQueue />)
    fireEvent.click(await screen.findByRole('button', { name: 'Reject' }))
    const reason = screen.getByLabelText('Reason for rejecting Enoxaparin for Asha Rao') as HTMLTextAreaElement
    fireEvent.change(reason, { target: { value: 'Duplicate anticoagulant' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm rejection' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Rejection failed: the medicine was already stopped')
    expect(reason.value).toBe('Duplicate anticoagulant')
    expect(screen.getByText('Enoxaparin')).toBeInTheDocument()
  })
})
