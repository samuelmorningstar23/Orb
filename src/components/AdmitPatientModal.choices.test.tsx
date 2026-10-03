/**
 * Admit modal: department, attending doctor, bed and age (gap register F075).
 *
 * All four were free text. A typed department put the patient on nobody's
 * board, a typed doctor id failed as a raw error after the whole form was
 * filled, two patients ended in one bed with no word said, and any age was
 * accepted. Department and attending now pick from GET /api/admissions/options
 * (the server checks them again), a held bed is WARNED, never blocked (the
 * founder's call), and age must be a whole number from 0 to 130.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const admitPatient = vi.fn()
const apiFetch = vi.fn()
vi.mock('../api/client', () => ({
  admitPatient: (...a: unknown[]) => admitPatient(...a),
  apiFetch: (...a: unknown[]) => apiFetch(...a),
}))
vi.mock('../App', () => ({
  useAuth: () => ({
    user: { id: 'D001', name: 'Dr. Test', role: 'doctor', department: 'general medicine' },
  }),
}))

import AdmitPatientModal from './AdmitPatientModal'
import { ageInRange } from './admissionAge'

const OPTIONS = {
  departments: ['Cardiology', 'General Medicine'],
  default_department: 'General Medicine',
  doctors: [
    { id: 'D001', name: 'Dr. Test', department: 'General Medicine' },
    { id: 'D002', name: 'Dr. Heart', department: 'Cardiology' },
  ],
  wards: [
    { ward: 'GM', departments: ['General Medicine'] },
    { ward: 'CCU', departments: ['Cardiology'] },
  ],
  beds: [
    { ward: 'GM', bed: 'GM-101', in_use: true },
    { ward: 'GM', bed: 'GM-102', in_use: false },
  ],
}

function openAdmissionStep(container: HTMLElement) {
  fireEvent.change(container.querySelector('input.admit-input[type="text"]') as HTMLInputElement, {
    target: { value: 'Asha Menon' },
  })
  fireEvent.change(container.querySelector('input.admit-input[type="number"]') as HTMLInputElement, {
    target: { value: '61' },
  })
  fireEvent.change(container.querySelector('select.admit-input') as HTMLSelectElement, { target: { value: 'F' } })
  fireEvent.click(screen.getByRole('button', { name: /^Admission/ }))
}

function fillClinical(container: HTMLElement) {
  fireEvent.click(screen.getByRole('button', { name: /^Clinical/ }))
  fireEvent.change(container.querySelector('textarea.admit-input') as HTMLTextAreaElement, {
    target: { value: 'Fever and cough' },
  })
}

describe('AdmitPatientModal: choices the server also checks', () => {
  beforeEach(() => {
    admitPatient.mockReset()
    apiFetch.mockReset()
    apiFetch.mockResolvedValue(OPTIONS)
    admitPatient.mockResolvedValue({ patient_login_id: 'PT1', temporary_password: 'x', date_of_birth_recorded: true })
  })

  it('reads the lists from the admission options and defaults to the clinician, in the listed spelling', async () => {
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
    openAdmissionStep(container)
    expect(apiFetch).toHaveBeenCalledWith('/admissions/options')
    // The clinician's own "general medicine" is shown as the listed "General Medicine".
    await waitFor(() => expect(screen.getByLabelText(/^Department/)).toHaveValue('General Medicine'))
    expect(screen.getByLabelText(/^Attending Doctor/)).toHaveValue('D001')
    expect(screen.getByRole('option', { name: 'Dr. Heart (Cardiology)' })).toBeInTheDocument()
    // Department and attending are pickers now, not text boxes.
    expect(screen.getByLabelText(/^Department/).tagName).toBe('SELECT')
    expect(screen.getByLabelText(/^Attending Doctor/).tagName).toBe('SELECT')
  })

  it('a clinician who is not on the doctor list starts with no attending, and Review stays shut', async () => {
    apiFetch.mockResolvedValue({ ...OPTIONS, doctors: [OPTIONS.doctors[1]] })
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
    openAdmissionStep(container)
    await waitFor(() => expect(screen.getByLabelText(/^Department/)).toHaveValue('General Medicine'))
    expect(screen.getByLabelText(/^Attending Doctor/)).toHaveValue('')
    fireEvent.change(screen.getByLabelText(/^Ward/), { target: { value: 'GM' } })
    fillClinical(container)
    expect(screen.getByRole('button', { name: 'Review Admission' })).toBeDisabled()
  })

  it('when the lists cannot load it says so, offers a retry, and admits nobody', async () => {
    apiFetch.mockRejectedValueOnce(new Error('offline'))
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
    openAdmissionStep(container)
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load the departments and doctors')
    fireEvent.change(screen.getByLabelText(/^Ward/), { target: { value: 'GM' } })
    fillClinical(container)
    expect(screen.getByRole('button', { name: 'Review Admission' })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: /^Admission/ }))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Try again' })) })
    await waitFor(() => expect(screen.getByLabelText(/^Attending Doctor/)).toHaveValue('D001'))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('a bed already held is warned, not blocked, and the server warning shows after admission', async () => {
    admitPatient.mockResolvedValue({
      patient_login_id: 'PT1', temporary_password: 'x', date_of_birth_recorded: true,
      bed_warning: 'Bed GM-101 on GM is already held by another admitted patient. Check the bed with the ward.',
    })
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
    openAdmissionStep(container)
    await waitFor(() => expect(screen.getByLabelText(/^Attending Doctor/)).toHaveValue('D001'))
    fireEvent.change(screen.getByLabelText(/^Ward/), { target: { value: 'gm' } })

    fireEvent.change(screen.getByLabelText(/^Bed/), { target: { value: 'GM-102' } })
    expect(screen.queryByText(/already held by an admitted patient/)).not.toBeInTheDocument()

    fireEvent.change(screen.getByLabelText(/^Bed/), { target: { value: 'gm-101 ' } })
    expect(screen.getByRole('status')).toHaveTextContent('This bed is already held by an admitted patient.')

    fillClinical(container)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Review Admission' })) })
    expect(screen.getByText('Already held by an admitted patient')).toBeInTheDocument()
    // The attending is named, not shown as an internal id.
    expect(screen.getByText('Dr. Test')).toBeInTheDocument()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm Admission' })) })

    expect(admitPatient).toHaveBeenCalledTimes(1)
    expect(admitPatient.mock.calls[0][0]).toMatchObject({
      department: 'General Medicine', attending_doctor_id: 'D001', bed: 'gm-101 ',
    })
    expect(screen.getByText(/Bed GM-101 on GM is already held/)).toBeInTheDocument()
  })

  it('an age outside 0 to 130 keeps Identity invalid and says why', () => {
    // Age lives on the Identity step; the lists never need to arrive.
    apiFetch.mockReturnValue(new Promise(() => {}))
    const { container } = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
    const age = container.querySelector('input.admit-input[type="number"]') as HTMLInputElement
    for (const bad of ['-4', '131', '4.5']) {
      fireEvent.change(age, { target: { value: bad } })
      expect(screen.getByText('Age must be a whole number from 0 to 130.')).toBeInTheDocument()
    }
    fireEvent.change(age, { target: { value: '130' } })
    expect(screen.queryByText('Age must be a whole number from 0 to 130.')).not.toBeInTheDocument()
  })
})

describe('ageInRange', () => {
  it('accepts whole years from 0 to 130 only', () => {
    expect(['0', '7', '130', ' 45 '].map(ageInRange)).toEqual([true, true, true, true])
    expect(['-1', '131', '4.5', '', 'abc', '1e2'].map(ageInRange)).toEqual([false, false, false, false, false, false])
  })
})
