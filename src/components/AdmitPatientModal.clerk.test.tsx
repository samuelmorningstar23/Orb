/**
 * The admission form in the ward clerk's registration-only mode (adt-17b,
 * ruling ADT-S5 (a), a build default for Samuel to confirm).
 *
 * What it must never get wrong:
 *
 * 1. NOTHING CLINICAL. The form has no clinical, history, observation or
 *    notes tab, and the request it sends carries no complaint, diagnosis,
 *    allergy, history, medicine, observation, note, isolation precaution or
 *    code status: the clerk can never record an allergy, or 'none'.
 * 2. ITS OWN LISTS AND DOOR. It reads /admissions/clerk-options and sends to
 *    /admissions/clerk-admit with one Idempotency-Key per form; the department
 *    is the clerk's own and cannot be changed here.
 * 3. THE HOSPITAL'S OWN NUMBER is required, unless the site issues it.
 * 4. THE REVIEW SAYS the clinical details are not yet assessed and allergies
 *    are not recorded as none.
 * 5. STRICT LOCATIONS: the server's refusal of a place not in bed setup is
 *    shown by the bed picker.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetModuleFlags, setModuleFlags } from '../shellConfig'

const admitPatient = vi.hoisted(() => vi.fn())
const apiFetch = vi.hoisted(() => vi.fn())
vi.mock('../api/client', () => ({
  admitPatient: (...a: unknown[]) => admitPatient(...a),
  apiFetch: (...a: unknown[]) => apiFetch(...a),
}))
vi.mock('../App', () => ({
  useAuth: () => ({
    user: { id: 'WC1', name: 'Clerk Test', role: 'ward_clerk', department: 'General Medicine' },
  }),
}))

import AdmitPatientModal from './AdmitPatientModal'

const MASTER = {
  backtime_hours: 72,
  authorisers: [],
  self_authoriser: null,
  strict_locations: false,
  wards: [{
    id: 'WRD-GM', code: 'GM-1', name: null, department: 'General Medicine', ward_type: 'general',
    cleaning_tracked: false, register_complete: true,
    beds: [{
      bed_id: 'BED-7', bed_label: '7', unit_code: 'GM-1/7', room: null, bed_class: null,
      isolation: false, temporary: false, status: 'available', status_words: 'Available',
      tone: 'ready', markers: [], occupied: false, selectable: true, needs: null,
      terminal_clean: false,
    }],
  }],
}

const CLERK_OPTIONS = {
  department: 'General Medicine',
  departments: ['General Medicine'],
  default_department: 'General Medicine',
  doctors: [{ id: 'D001', name: 'Dr. Test', department: 'General Medicine' }],
  master: MASTER,
  clinical_fields: 'Not yet assessed',
  hospital_number_issued: false,
}

const ADMITTED = {
  success: true, patient_id: 'P1', admission_id: 'ADM-1', channel_id: 'c1', mrn: 'HN-1',
  patient_login_id: 'PT123', temporary_password: 'x', date_of_birth_recorded: true,
  bed_warning: null, replayed: false, location_warning: null, clerk_admission: true,
  assessment_due: true,
}

const CLINICAL_KEYS = ['chief_complaint', 'initial_diagnosis', 'triage_severity', 'allergies',
  'comorbidities', 'current_medications', 'past_medical_history', 'past_surgical_history',
  'family_history', 'social_history', 'vitals', 'admitting_notes', 'nursing_notes',
  'isolation_precautions', 'code_status']

let options: Record<string, unknown> = CLERK_OPTIONS
let admitReply: () => Promise<unknown> = () => Promise.resolve(ADMITTED)

function clerkAdmitCalls() {
  return apiFetch.mock.calls.filter(([path]) => path === '/admissions/clerk-admit')
}

beforeEach(() => {
  resetModuleFlags()
  setModuleFlags({ adt: true })
  options = CLERK_OPTIONS
  admitReply = () => Promise.resolve(ADMITTED)
  admitPatient.mockReset()
  apiFetch.mockReset()
  apiFetch.mockImplementation((path: string) => {
    if (path === '/admissions/clerk-options') return Promise.resolve(options)
    if (path === '/admissions/clerk-admit') return admitReply()
    return Promise.reject(new Error(`unexpected ${path}`))
  })
})

afterEach(() => {
  cleanup()
  resetModuleFlags()
})

async function fillIdentity(container: HTMLElement, mrn = 'HN-0042') {
  const name = container.querySelector('input.admit-input[type="text"]') as HTMLInputElement
  fireEvent.change(name, { target: { value: 'Asha Menon' } })
  const age = container.querySelector('input.admit-input[type="number"]') as HTMLInputElement
  fireEvent.change(age, { target: { value: '61' } })
  fireEvent.change(container.querySelector('select.admit-input') as HTMLSelectElement, { target: { value: 'F' } })
  const number = screen.queryByLabelText(/^Hospital number/)
  if (number && mrn) fireEvent.change(number, { target: { value: mrn } })
}

async function chooseBed() {
  fireEvent.click(screen.getByRole('button', { name: /^Admission/ }))
  await waitFor(() => expect(screen.getByLabelText(/^Attending Doctor/)).toHaveValue(''))
  fireEvent.change(screen.getByLabelText(/^Attending Doctor/), { target: { value: 'D001' } })
  fireEvent.change(screen.getByLabelText('Ward'), { target: { value: 'WRD-GM' } })
  fireEvent.click(screen.getByRole('radio', { name: /Bed 7/ }))
}

async function reviewAndConfirm() {
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Review Admission' })) })
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm Admission' })) })
}

describe("AdmitPatientModal in the ward clerk's mode", () => {
  it('reads the clerk options and offers no clinical tab', async () => {
    render(<AdmitPatientModal clerk onClose={() => {}} onSuccess={() => {}} />)
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/admissions/clerk-options'))
    expect(screen.getByRole('heading', { name: 'Admit a patient' })).toBeInTheDocument()
    for (const tab of ['Clinical', 'History', 'Vitals', 'Notes']) {
      expect(screen.queryByRole('button', { name: new RegExp(`^${tab}`) })).toBeNull()
    }
    expect(screen.getByRole('button', { name: /^Identity/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Review' })).toBeInTheDocument()
  })

  it('sends registration only, to the clerk door, with one key', async () => {
    const { container } = render(<AdmitPatientModal clerk onClose={() => {}} onSuccess={() => {}} />)
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/admissions/clerk-options'))
    await fillIdentity(container)
    await chooseBed()
    expect(screen.getByLabelText(/^Department/)).toBeDisabled()
    expect(screen.getByLabelText(/^Department/)).toHaveValue('General Medicine')
    await reviewAndConfirm()
    expect(admitPatient).not.toHaveBeenCalled()
    expect(clerkAdmitCalls()).toHaveLength(1)
    const [, init] = clerkAdmitCalls()[0] as [string, RequestInit]
    expect((init.headers as Record<string, string>)['Idempotency-Key']).toMatch(/\S{8,}/)
    const body = JSON.parse(String(init.body))
    expect(body).toMatchObject({
      name: 'Asha Menon', age: 61, sex: 'F', mrn: 'HN-0042', department: 'General Medicine',
      attending_doctor_id: 'D001', ward: 'GM-1', bed: '7', ward_id: 'WRD-GM', bed_id: 'BED-7',
    })
    for (const key of CLINICAL_KEYS) expect(body).not.toHaveProperty(key)
  })

  it('needs the hospital number unless the site issues it', async () => {
    const { container } = render(<AdmitPatientModal clerk onClose={() => {}} onSuccess={() => {}} />)
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/admissions/clerk-options'))
    await fillIdentity(container, '')
    await chooseBed()
    expect(screen.getByRole('button', { name: 'Review Admission' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: /^Identity/ }))
    fireEvent.change(screen.getByLabelText(/^Hospital number/), { target: { value: 'HN-9' } })
    expect(screen.getByRole('button', { name: 'Review Admission' })).toBeEnabled()
  })

  it('asks for no number when the site issues it, and sends none', async () => {
    options = { ...CLERK_OPTIONS, hospital_number_issued: true }
    const { container } = render(<AdmitPatientModal clerk onClose={() => {}} onSuccess={() => {}} />)
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/admissions/clerk-options'))
    await waitFor(() => expect(screen.queryByLabelText(/^Hospital number/)).toBeNull())
    await fillIdentity(container)
    await chooseBed()
    await reviewAndConfirm()
    const body = JSON.parse(String((clerkAdmitCalls()[0] as [string, RequestInit])[1].body))
    expect(body).not.toHaveProperty('mrn')
  })

  it('says on review that the clinical details are not yet assessed, never none', async () => {
    const { container } = render(<AdmitPatientModal clerk onClose={() => {}} onSuccess={() => {}} />)
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/admissions/clerk-options'))
    await fillIdentity(container)
    await chooseBed()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Review Admission' })) })
    const note = screen.getByText(/^Not yet assessed\./)
    expect(note).toHaveTextContent('never as none')
    expect(note).toHaveTextContent('Admission assessment due')
    expect(screen.queryByText(/NKDA/)).toBeNull()
  })

  it('offers no typed place while strict locations are on, and does while they are off', async () => {
    const { container, unmount } = render(<AdmitPatientModal clerk onClose={() => {}} onSuccess={() => {}} />)
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/admissions/clerk-options'))
    await fillIdentity(container)
    await chooseBed()
    expect(screen.getByRole('option', { name: 'A ward not in bed setup' })).toBeInTheDocument()
    unmount()
    options = { ...CLERK_OPTIONS, master: { ...MASTER, strict_locations: true } }
    const again = render(<AdmitPatientModal clerk onClose={() => {}} onSuccess={() => {}} />)
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/admissions/clerk-options'))
    await fillIdentity(again.container)
    await chooseBed()
    expect(screen.queryByRole('option', { name: 'A ward not in bed setup' })).toBeNull()
  })

  it('shows a strict-locations refusal by the bed picker', async () => {
    admitReply = () => Promise.reject(Object.assign(new Error('Ward X is not in bed setup.'), {
      status: 400, detail: { error: 'ward_not_in_setup', message: 'Ward X is not in bed setup.' },
    }))
    const { container } = render(<AdmitPatientModal clerk onClose={() => {}} onSuccess={() => {}} />)
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/admissions/clerk-options'))
    await fillIdentity(container)
    await chooseBed()
    await reviewAndConfirm()
    expect(await screen.findAllByText('Ward X is not in bed setup.')).not.toHaveLength(0)
    // The beds are read again after a place refusal.
    await waitFor(() => expect(apiFetch.mock.calls.filter(([p]) => p === '/admissions/clerk-options'))
      .toHaveLength(2))
  })
})
