/**
 * Care bundle cancel: stopping a Sepsis Six protocol asks for a reason and sends
 * it to the server (which audits it), instead of a bare window.confirm that
 * recorded nothing.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const getPatientBundles = vi.fn()
const cancelBundle = vi.fn()

vi.mock('../../api/bundles', () => ({
  getPatientBundles: (...a: unknown[]) => getPatientBundles(...a),
  startBundle: vi.fn(),
  completeBundleElement: vi.fn(),
  cancelBundle: (...a: unknown[]) => cancelBundle(...a),
}))

import CareBundleTimer from './CareBundleTimer'

const bundle = {
  id: 7,
  patient_id: 'P-1',
  bundle_type: 'sepsis6',
  status: 'active',
  triggered_at: '2026-10-01 08:00:00',
  triggered_by: 'U1',
  trigger_reason: 'NEWS2 7',
  source: 'clinician',
  elements: [
    { key: 'oxygen', label: 'Give high-flow oxygen', target_min: 60, completed_at: null, state: 'pending' },
  ],
  completed_count: 0,
  total_count: 6,
  completion_pct: 0,
  on_track: true,
}

describe('CareBundleTimer cancel', () => {
  beforeEach(() => {
    getPatientBundles.mockReset()
    cancelBundle.mockReset()
    getPatientBundles.mockResolvedValue([bundle])
  })

  it('never uses window.confirm and requires a reason before cancelling', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm')
    cancelBundle.mockResolvedValue({ ok: true })
    render(<CareBundleTimer patientId="P-1" />)

    fireEvent.click(await screen.findByRole('button', { name: 'Cancel bundle' }))
    expect(confirmSpy).not.toHaveBeenCalled()

    const submit = screen.getByRole('button', { name: 'Cancel bundle' })
    expect(submit).toBeDisabled()
    fireEvent.change(screen.getByLabelText(/Reason for cancelling/), { target: { value: '   ' } })
    expect(submit).toBeDisabled()

    fireEvent.change(screen.getByLabelText(/Reason for cancelling/), {
      target: { value: '  Sepsis excluded, urinary retention  ' },
    })
    expect(submit).toBeEnabled()
    fireEvent.click(submit)

    await waitFor(() => expect(cancelBundle).toHaveBeenCalledWith(7, 'Sepsis excluded, urinary retention'))
    confirmSpy.mockRestore()
  })

  it('keeping the bundle closes the form without calling the server', async () => {
    render(<CareBundleTimer patientId="P-1" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel bundle' }))
    fireEvent.click(screen.getByRole('button', { name: 'Keep bundle' }))
    expect(screen.queryByLabelText(/Reason for cancelling/)).not.toBeInTheDocument()
    expect(cancelBundle).not.toHaveBeenCalled()
  })

  it('shows the server refusal instead of swallowing it', async () => {
    cancelBundle.mockRejectedValue(new Error('This care bundle is no longer active.'))
    render(<CareBundleTimer patientId="P-1" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel bundle' }))
    fireEvent.change(screen.getByLabelText(/Reason for cancelling/), { target: { value: 'Duplicate' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel bundle' }))
    expect(await screen.findByText('This care bundle is no longer active.')).toBeInTheDocument()
  })
})
