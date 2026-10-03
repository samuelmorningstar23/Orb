/**
 * Admit modal: the one-time portal login screen (F144).
 *
 * It reused the form's 90vh dialog and side-by-side body, so the login card and
 * the Done row stood as two tall columns. Worse, a click on the backdrop
 * finished the screen, and the password can never be shown again. Now the
 * screen is a compact column and only "Done" leaves while a password is shown.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const admitPatient = vi.fn()
const OPTIONS = {
  departments: ['General Medicine'],
  default_department: 'General Medicine',
  doctors: [{ id: 'D001', name: 'Dr. Test', department: 'General Medicine' }],
  wards: [{ ward: 'GM', departments: ['General Medicine'] }],
  beds: [],
}
vi.mock('../api/client', () => ({
  admitPatient: (...a: unknown[]) => admitPatient(...a),
  apiFetch: () => Promise.resolve(OPTIONS),
}))
vi.mock('../App', () => ({
  useAuth: () => ({
    user: { id: 'D001', name: 'Dr. Test', role: 'doctor', department: 'General Medicine' },
  }),
}))

import AdmitPatientModal from './AdmitPatientModal'

async function admit(onSuccess: () => void) {
  const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={onSuccess} />)
  fireEvent.change(container.querySelector('input.admit-input[type="text"]') as HTMLInputElement, { target: { value: 'Asha Menon' } })
  fireEvent.change(container.querySelector('input.admit-input[type="number"]') as HTMLInputElement, { target: { value: '61' } })
  fireEvent.change(container.querySelector('select.admit-input') as HTMLSelectElement, { target: { value: 'F' } })
  fireEvent.click(screen.getByRole('button', { name: /^Admission/ }))
  await waitFor(() => expect(screen.getByLabelText(/^Attending Doctor/)).toHaveValue('D001'))
  fireEvent.change(screen.getByLabelText(/^Ward/), { target: { value: 'GM' } })
  fireEvent.click(screen.getByRole('button', { name: /^Clinical/ }))
  fireEvent.change(container.querySelector('textarea.admit-input') as HTMLTextAreaElement, { target: { value: 'Fever' } })
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Review Admission' })) })
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm Admission' })) })
  return container
}

describe('AdmitPatientModal: one-time login screen', () => {
  beforeEach(() => { admitPatient.mockReset() })

  it('is a compact column, not the form layout', async () => {
    admitPatient.mockResolvedValue({ patient_login_id: 'PT9', temporary_password: 'pw-123', date_of_birth_recorded: true })
    const container = await admit(() => {})
    expect(screen.getByText('pw-123')).toBeInTheDocument()
    const dialog = screen.getByRole('dialog')
    expect(dialog).toHaveClass('admit-modal--credential')
    expect(dialog.querySelector('.admit-modal__body')).toHaveClass('admit-modal__body--stack')
    expect(container.querySelector('.admit-modal__summary')).toBeNull()
  })

  it('a click outside or Escape keeps the password on screen; only Done finishes', async () => {
    admitPatient.mockResolvedValue({ patient_login_id: 'PT9', temporary_password: 'pw-123', date_of_birth_recorded: true })
    const onSuccess = vi.fn()
    const container = await admit(onSuccess)

    fireEvent.click(container.querySelector('.admit-modal-backdrop') as HTMLElement)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onSuccess).not.toHaveBeenCalled()
    expect(screen.getByText('pw-123')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    expect(onSuccess).toHaveBeenCalledTimes(1)
  })

  it('with no password to lose, Escape still finishes', async () => {
    admitPatient.mockResolvedValue({ date_of_birth_recorded: false })
    const onSuccess = vi.fn()
    await admit(onSuccess)
    expect(screen.getByRole('note')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onSuccess).toHaveBeenCalledTimes(1)
  })
})
