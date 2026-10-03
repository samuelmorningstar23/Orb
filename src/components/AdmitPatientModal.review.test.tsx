/**
 * Admit modal: the Review step is a real step, and the date of birth is sent.
 *
 * "Review Admission" used to admit the patient on the spot. Its button and
 * "Confirm Admission" shared one slot with no keys, so React reused the node and
 * flipped it to type="submit" during the click; the form then submitted and the
 * Review step was never seen. Admission now happens only from Confirm, on Review.
 *
 * Date of birth: optional (an admission never waits on paperwork), but it is the
 * second factor on the discharge link, so it is sent when typed and its absence
 * is said on the success screen.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const admitPatient = vi.fn()
// What GET /api/admissions/options serves: the department, attending and ward
// fields pick from these (see AdmitPatientModal.choices.test.tsx).
const OPTIONS = {
  departments: ['Cardiology', 'General Medicine'],
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
import { ageFromDob } from './admissionAge'

async function fillRequired(container: HTMLElement, { dob }: { dob?: string } = {}) {
  const name = container.querySelector('input.admit-input[type="text"]') as HTMLInputElement
  fireEvent.change(name, { target: { value: 'Asha Menon' } })
  if (dob) fireEvent.change(screen.getByLabelText('Date of Birth'), { target: { value: dob } })
  const age = container.querySelector('input.admit-input[type="number"]') as HTMLInputElement
  if (!age.value) fireEvent.change(age, { target: { value: '61' } })
  fireEvent.change(container.querySelector('select.admit-input') as HTMLSelectElement, { target: { value: 'F' } })

  fireEvent.click(screen.getByRole('button', { name: /^Admission/ }))
  // Department and attending default to the clinician's own once the lists load.
  await waitFor(() => expect(screen.getByLabelText(/^Attending Doctor/)).toHaveValue('D001'))
  fireEvent.change(screen.getByLabelText(/^Ward/), { target: { value: 'GM' } })

  fireEvent.click(screen.getByRole('button', { name: /^Clinical/ }))
  fireEvent.change(container.querySelector('textarea.admit-input') as HTMLTextAreaElement, {
    target: { value: 'Fever and cough' },
  })
}

describe('AdmitPatientModal: Review is a real step', () => {
  beforeEach(() => {
    admitPatient.mockReset()
    admitPatient.mockResolvedValue({
      patient_login_id: 'PT123', temporary_password: 'x', date_of_birth_recorded: true,
    })
  })

  it('clicking Review Admission shows the review and admits nobody', async () => {
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
    await fillRequired(container)

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Review Admission' })) })

    expect(admitPatient).not.toHaveBeenCalled()
    expect(screen.getByRole('heading', { name: 'Patient Identity' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Confirm Admission' })).toHaveAttribute('type', 'button')
  })

  it('Enter in a field never submits the admission', async () => {
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
    await fillRequired(container)
    await act(async () => {
      fireEvent.submit(container.querySelector('form#admit-form') as HTMLFormElement)
    })
    expect(admitPatient).not.toHaveBeenCalled()
  })

  it('Confirm Admission on the Review step admits once', async () => {
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
    await fillRequired(container)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Review Admission' })) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm Admission' })) })
    expect(admitPatient).toHaveBeenCalledTimes(1)
  })
})

describe('AdmitPatientModal: date of birth', () => {
  beforeEach(() => { admitPatient.mockReset() })

  it('posts date_of_birth and derives Age from it', async () => {
    admitPatient.mockResolvedValue({
      patient_login_id: 'PT123', temporary_password: 'x', date_of_birth_recorded: true,
    })
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
    await fillRequired(container, { dob: '1965-03-14' })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Review Admission' })) })
    expect(screen.getByText('14 Mar 1965')).toBeInTheDocument()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm Admission' })) })

    const payload = admitPatient.mock.calls[0][0]
    expect(payload.date_of_birth).toBe('1965-03-14')
    expect(payload.age).toBe(ageFromDob('1965-03-14'))
  })

  it('says no discharge link can be issued when no date of birth was recorded', async () => {
    admitPatient.mockResolvedValue({
      patient_login_id: 'PT124', temporary_password: 'y', date_of_birth_recorded: false,
    })
    const onSuccess = vi.fn()
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={onSuccess} />)
    await fillRequired(container)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Review Admission' })) })
    expect(screen.getByText('Not recorded (no discharge link)')).toBeInTheDocument()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm Admission' })) })

    expect(admitPatient.mock.calls[0][0].date_of_birth).toBeUndefined()
    expect(screen.getByRole('note').textContent).toMatch(/no discharge link can be issued/i)
    expect(onSuccess).not.toHaveBeenCalled()
  })

  it('stops on the success screen for a missing date of birth even with no portal login', async () => {
    admitPatient.mockResolvedValue({ date_of_birth_recorded: false })
    const onSuccess = vi.fn()
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={onSuccess} />)
    await fillRequired(container)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Review Admission' })) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm Admission' })) })
    expect(screen.getByRole('note')).toBeInTheDocument()
    expect(screen.queryByText('One-time password')).not.toBeInTheDocument()
    expect(onSuccess).not.toHaveBeenCalled()
  })

  it('keeps an infant age of 0 instead of dropping it', async () => {
    admitPatient.mockResolvedValue({ temporary_password: 'z', date_of_birth_recorded: true })
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
    const age = container.querySelector('input.admit-input[type="number"]') as HTMLInputElement
    fireEvent.change(age, { target: { value: '0' } })
    await fillRequired(container)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Review Admission' })) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm Admission' })) })
    expect(admitPatient.mock.calls[0][0].age).toBe(0)
  })
})

describe('ageFromDob', () => {
  it('counts whole years to the day', () => {
    expect(ageFromDob('1965-03-14', '2026-03-13')).toBe(60)
    expect(ageFromDob('1965-03-14', '2026-03-14')).toBe(61)
    expect(ageFromDob('2026-10-02', '2026-10-02')).toBe(0)
  })

  it('refuses dates the server would not keep', () => {
    expect(ageFromDob('2026-10-03', '2026-10-02')).toBeNull()
    expect(ageFromDob('1899-12-31', '2026-10-02')).toBeNull()
    expect(ageFromDob('2023-02-30', '2026-10-02')).toBeNull()
  })
})
