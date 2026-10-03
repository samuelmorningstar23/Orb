/**
 * Admit modal: ACVPU and supplemental O2 start "Not assessed" and are sent as null.
 *
 * Both used to start at Alert and room air, so a form nobody touched recorded
 * an assessment nobody made. The server stored it, NEWS2 scored the admission
 * set as complete, and a confused patient on oxygen was admitted up to 5 points
 * low (3 for ACVPU, 2 for O2). Ruled 2026-10-03. The server contract
 * (admission_service): consciousness null/""/missing stores NULL; on_oxygen
 * null/missing stores NULL, while "" would be stored as on air, so the form
 * must send null, never "".
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const admitPatient = vi.fn()
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

async function fillRequired(container: HTMLElement) {
  const name = container.querySelector('input.admit-input[type="text"]') as HTMLInputElement
  fireEvent.change(name, { target: { value: 'Asha Menon' } })
  const age = container.querySelector('input.admit-input[type="number"]') as HTMLInputElement
  fireEvent.change(age, { target: { value: '61' } })
  fireEvent.change(container.querySelector('select.admit-input') as HTMLSelectElement, { target: { value: 'F' } })

  fireEvent.click(screen.getByRole('button', { name: /^Admission/ }))
  await waitFor(() => expect(screen.getByLabelText(/^Attending Doctor/)).toHaveValue('D001'))
  fireEvent.change(screen.getByLabelText(/^Ward/), { target: { value: 'GM' } })

  fireEvent.click(screen.getByRole('button', { name: /^Clinical/ }))
  fireEvent.change(container.querySelector('textarea.admit-input') as HTMLTextAreaElement, {
    target: { value: 'Fever and cough' },
  })
}

/** The Vitals tab with a heart rate typed, so the vitals block is sent. */
function openVitalsWithHeartRate(container: HTMLElement) {
  fireEvent.click(screen.getByRole('button', { name: /^Vitals/ }))
  const hr = container.querySelector('.admit-modal__section input[type="number"]') as HTMLInputElement
  fireEvent.change(hr, { target: { value: '96' } })
}

async function admit() {
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Review Admission' })) })
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm Admission' })) })
  expect(admitPatient).toHaveBeenCalledTimes(1)
  return admitPatient.mock.calls[0][0]
}

describe('AdmitPatientModal: ACVPU and O2 are not assumed', () => {
  beforeEach(() => {
    admitPatient.mockReset()
    admitPatient.mockResolvedValue({
      patient_login_id: 'PT123', temporary_password: 'x', date_of_birth_recorded: true,
    })
  })

  it('both start at Not assessed', async () => {
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
    await fillRequired(container)
    openVitalsWithHeartRate(container)

    const acvpu = screen.getByLabelText('Consciousness (ACVPU)') as HTMLSelectElement
    const oxygen = screen.getByLabelText('On supplemental O₂') as HTMLSelectElement
    expect(acvpu).toHaveValue('')
    expect(acvpu.selectedOptions[0].textContent).toBe('Not assessed')
    expect(oxygen).toHaveValue('')
    expect(oxygen.selectedOptions[0].textContent).toBe('Not assessed')
  })

  it('an untouched form sends null for both, never alert or on air', async () => {
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
    await fillRequired(container)
    openVitalsWithHeartRate(container)

    const payload = await admit()
    expect(payload.vitals.hr).toBe(96)
    expect(payload.vitals.consciousness).toBeNull()
    expect(payload.vitals.on_oxygen).toBeNull()
  })

  it('a recorded assessment is sent as recorded', async () => {
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
    await fillRequired(container)
    openVitalsWithHeartRate(container)
    fireEvent.change(screen.getByLabelText('Consciousness (ACVPU)'), { target: { value: 'confusion' } })
    fireEvent.change(screen.getByLabelText('On supplemental O₂'), { target: { value: 'yes' } })

    const payload = await admit()
    expect(payload.vitals.consciousness).toBe('confusion')
    expect(payload.vitals.on_oxygen).toBe(true)
  })

  it('room air is a recorded false, not a missing value', async () => {
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
    await fillRequired(container)
    openVitalsWithHeartRate(container)
    fireEvent.change(screen.getByLabelText('Consciousness (ACVPU)'), { target: { value: 'alert' } })
    fireEvent.change(screen.getByLabelText('On supplemental O₂'), { target: { value: 'no' } })

    const payload = await admit()
    expect(payload.vitals.consciousness).toBe('alert')
    expect(payload.vitals.on_oxygen).toBe(false)
  })
})
