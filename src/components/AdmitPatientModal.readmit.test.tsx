/**
 * The admission form readmitting a discharged patient onto their own record
 * (opd-17c).
 *
 * What it must never get wrong:
 *
 * 1. ONLY WHAT THE SERVER OFFERS. A new admission the patient index refuses
 *    offers readmission only for the records the server marks readmittable,
 *    and nothing while readmission is switched off (no readmission_available).
 * 2. IDENTITY IS FIXED. The record's name and UHID are shown, never edited;
 *    the clinician types what the patient STATES (ruling OPD-S1) and the form
 *    cannot be reviewed until it is enough for the server's check. No
 *    identity field, no date of birth and no observations are sent.
 * 3. ONE KEY PER READMISSION, sent to the readmission route.
 * 4. THE EARLIER STAY'S MEDICINES (ruling OPD-S8): the result lists what was
 *    stopped for the prescriber to prescribe again, or counts it when the
 *    server does not name it; the fresh portal password is shown once.
 * 5. A clinic visit whose patient was an inpatient before says what admitting
 *    them does, in the server's words.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { readmitCandidatesOf } from '../api/readmission'
import { resetClinicalSwitches, resetModuleFlags } from '../shellConfig'

const apiFetch = vi.hoisted(() => vi.fn())
const admitPatient = vi.hoisted(() => vi.fn())
vi.mock('../api/client', () => ({
  admitPatient: (...a: unknown[]) => admitPatient(...a),
  apiFetch: (...a: unknown[]) => apiFetch(...a),
}))
vi.mock('../App', () => ({
  useAuth: () => ({
    user: { id: 'D001', name: 'Dr. Test', role: 'doctor', department: 'General Medicine' },
  }),
}))

import AdmitPatientModal from './AdmitPatientModal'

const OPTIONS = {
  departments: ['General Medicine'],
  default_department: 'General Medicine',
  doctors: [{ id: 'D001', name: 'Dr. Test', department: 'General Medicine' }],
  wards: [{ ward: 'GM', departments: ['General Medicine'] }],
  beds: [],
}

const CANDIDATE = {
  patient_id: 'PAT-9', uhid: 'UH-0909', name: 'Ravi Kumar', sex: 'M', age: 61,
  age_stated: false, phone_last4: '4321', last_visit_date: '2026-10-01', inpatient_now: false,
  score: 100, band: 'hard', reasons: ['uhid'], reason_labels: ['Same hospital number'],
  readmittable: true,
}

function refusal(detail: Record<string, unknown>) {
  const err = new Error(String(detail.message)) as Error & { status?: number; detail?: unknown }
  err.status = 409
  err.detail = detail
  return err
}

const REFUSED = {
  error: 'possible_existing_patient', reason: 'existing_record',
  message: 'A patient with this hospital number already has a record. If this is a patient who was an inpatient here before, readmit them onto their own record instead: choose Readmit and confirm who they are.',
  candidates: [CANDIDATE, { ...CANDIDATE, patient_id: 'PAT-8', name: 'Not Readmittable', readmittable: false }],
  candidate_count: 2, candidates_hidden: false, mode: 'block', check_incomplete: false,
  override_allowed: false, admit_existing_available: false, readmission_available: true,
}

const READMITTED = {
  success: true, replayed: false, patient_id: 'PAT-9', admission_id: 'ADM-R', channel_id: 'c9',
  mrn: 'UH-0909', patient_login_id: 'PT9', temporary_password: 'fresh-once',
  portal_login_created: false, date_of_birth_recorded: true, bed_warning: null,
  readmission: true, portal_password_reissued: true, previous_stay_medicines_stopped: 2,
  previous_stay_medicines_listed: true,
  previous_stay_medicines: [
    { medication_id: 1, drug_name: 'Amoxicillin', dose: '500 mg', route: 'Oral', frequency: 'TDS',
      as_needed: false, started_at: '2026-10-03 08:00:00', was_on_hold: false },
    { medication_id: 2, drug_name: 'Morphine', dose: '5 mg', route: 'IV', frequency: 'PRN',
      as_needed: true, started_at: '2026-10-03 09:00:00', was_on_hold: true },
  ],
}

let readmitReply: () => Promise<unknown> = () => Promise.resolve(READMITTED)

function readmitCalls() {
  return apiFetch.mock.calls.filter(([path]) => path === '/admissions/readmit')
}

beforeEach(() => {
  resetModuleFlags()
  readmitReply = () => Promise.resolve(READMITTED)
  admitPatient.mockReset()
  admitPatient.mockRejectedValue(refusal(REFUSED))
  apiFetch.mockReset()
  apiFetch.mockImplementation((path: string) => {
    if (path === '/admissions/options') return Promise.resolve(OPTIONS)
    if (path === '/admissions/readmit') return readmitReply()
    return Promise.reject(new Error(`unexpected ${path}`))
  })
})

afterEach(() => {
  cleanup()
  resetModuleFlags()
  resetClinicalSwitches()
})

async function tryNewAdmission() {
  const view = render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}} />)
  const { container } = view
  fireEvent.change(container.querySelector('input.admit-input[type="text"]') as HTMLInputElement, { target: { value: 'Ravi K' } })
  fireEvent.change(container.querySelector('input.admit-input[type="number"]') as HTMLInputElement, { target: { value: '61' } })
  fireEvent.change(container.querySelector('select.admit-input') as HTMLSelectElement, { target: { value: 'M' } })
  fireEvent.click(screen.getByRole('button', { name: /^Admission/ }))
  await waitFor(() => expect(screen.getByLabelText(/^Attending Doctor/)).toHaveValue('D001'))
  fireEvent.change(screen.getByLabelText(/^Ward/), { target: { value: 'GM' } })
  fireEvent.click(screen.getByRole('button', { name: /^Clinical/ }))
  fireEvent.change(container.querySelector('textarea.admit-input') as HTMLTextAreaElement, { target: { value: 'Breathless again' } })
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Review Admission' })) })
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm Admission' })) })
  return view
}

function stateIdentity() {
  fireEvent.change(screen.getByLabelText(/Name, as the patient says it/), { target: { value: 'Ravi Kumar' } })
  fireEvent.change(screen.getByLabelText(/^Date of birth/), { target: { value: '1965-02-01' } })
  fireEvent.change(screen.getByLabelText(/^Last 4 digits/), { target: { value: '4321' } })
}

describe('readmitCandidatesOf', () => {
  it('takes only the readmittable records of a refusal that offers readmission', () => {
    expect(readmitCandidatesOf(REFUSED).map(c => c.patient_id)).toEqual(['PAT-9'])
    expect(readmitCandidatesOf({ ...REFUSED, readmission_available: undefined })).toEqual([])
    expect(readmitCandidatesOf({ ...REFUSED, error: 'mrn_in_use' })).toEqual([])
    expect(readmitCandidatesOf(null)).toEqual([])
    expect(readmitCandidatesOf('text')).toEqual([])
  })
})

describe('AdmitPatientModal readmitting onto the same record', () => {
  it('offers readmission only for the records the server marks', async () => {
    await tryNewAdmission()
    expect(await screen.findByText(/This patient may already have a record/)).toBeInTheDocument()
    expect(screen.getByText('Ravi Kumar')).toBeInTheDocument()
    expect(screen.queryByText('Not Readmittable')).toBeNull()
    expect(screen.getAllByRole('button', { name: 'Readmit this patient' })).toHaveLength(1)
  })

  it('offers nothing while readmission is switched off', async () => {
    admitPatient.mockRejectedValue(refusal({ ...REFUSED, readmission_available: undefined,
      candidates: [{ ...CANDIDATE, readmittable: undefined }] }))
    await tryNewAdmission()
    await waitFor(() => expect(admitPatient).toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: 'Readmit this patient' })).toBeNull()
  })

  it('readmits with what the patient states, one key, and no identity or observations', async () => {
    await tryNewAdmission()
    fireEvent.click(await screen.findByRole('button', { name: 'Readmit this patient' }))
    expect(screen.getByRole('heading', { name: 'Readmit patient' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Ravi Kumar' })).toBeInTheDocument()
    expect(screen.getByText(/61 y, Male, UHID UH-0909/)).toBeInTheDocument()
    expect(screen.queryByText(/Full Name/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Review Admission' })).toBeDisabled()

    stateIdentity()
    fireEvent.click(screen.getByRole('button', { name: /^Observations/ }))
    expect(screen.getByText(/No observations are sent with a readmission/)).toBeInTheDocument()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Review Admission' })) })
    expect(screen.getByText('Checked against the record when you admit')).toBeInTheDocument()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm Admission' })) })

    expect(readmitCalls()).toHaveLength(1)
    const [, init] = readmitCalls()[0]
    const key = (init.headers as Record<string, string>)['Idempotency-Key']
    expect(key).toBeTruthy()
    const body = JSON.parse(init.body as string)
    expect(body.patient_id).toBe('PAT-9')
    expect(body.identity_check).toEqual({ name: 'Ravi Kumar', date_of_birth: '1965-02-01', phone_last4: '4321' })
    for (const field of ['name', 'age', 'sex', 'date_of_birth', 'mrn', 'phone', 'vitals']) {
      expect(body).not.toHaveProperty(field)
    }
    expect(body.department).toBe('General Medicine')
    expect(body.chief_complaint).toBe('Breathless again')

    expect(await screen.findByRole('heading', { name: /Patient readmitted/ })).toBeInTheDocument()
    expect(screen.getByText('fresh-once')).toBeInTheDocument()
    expect(screen.getByText(/The one from their earlier stay no longer works/)).toBeInTheDocument()
    expect(screen.getByText(/Amoxicillin 500 mg Oral TDS/)).toBeInTheDocument()
    expect(screen.getByText(/Morphine 5 mg IV when needed/)).toBeInTheDocument()
    expect(screen.getByText(/was on hold/)).toBeInTheDocument()
    expect(screen.getByText(/Prescribe each one again if the patient needs it/)).toBeInTheDocument()
  })

  it('a retry of the same readmission sends the same key', async () => {
    let calls = 0
    readmitReply = () => {
      calls += 1
      return calls === 1 ? Promise.reject(new Error('Network down')) : Promise.resolve(READMITTED)
    }
    await tryNewAdmission()
    fireEvent.click(await screen.findByRole('button', { name: 'Readmit this patient' }))
    stateIdentity()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Review Admission' })) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm Admission' })) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm Admission' })) })
    const keys = readmitCalls().map(([, init]) => (init.headers as Record<string, string>)['Idempotency-Key'])
    expect(keys).toHaveLength(2)
    expect(keys[0]).toBe(keys[1])
  })

  it('counts the stopped medicines when the server does not name them', async () => {
    readmitReply = () => Promise.resolve({ ...READMITTED, previous_stay_medicines_listed: false,
      previous_stay_medicines: [] })
    await tryNewAdmission()
    fireEvent.click(await screen.findByRole('button', { name: 'Readmit this patient' }))
    stateIdentity()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Review Admission' })) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm Admission' })) })
    expect(await screen.findByText(/2 medicines from the earlier stay were stopped/)).toBeInTheDocument()
    expect(screen.queryByText(/Amoxicillin/)).toBeNull()
  })

  it('"Not this patient" goes back to a new patient', async () => {
    await tryNewAdmission()
    fireEvent.click(await screen.findByRole('button', { name: 'Readmit this patient' }))
    fireEvent.click(screen.getByRole('button', { name: 'Not this patient' }))
    expect(screen.getByRole('heading', { name: 'Admit Patient' })).toBeInTheDocument()
    expect(screen.getByText(/Full Name/)).toBeInTheDocument()
  })
})

describe('A clinic visit whose patient was an inpatient before', () => {
  it('says what admitting them does, in the server’s words', async () => {
    const NOTE = 'This patient was an inpatient here before. Admitting them reopens their own record.'
    apiFetch.mockImplementation((path: string) => {
      if (path === '/admissions/options') return Promise.resolve(OPTIONS)
      if (path === '/admissions/admit-existing/visits/OPD-1') {
        return Promise.resolve({
          visit: { id: 'OPD-1', visit_date: '2026-10-09', department: 'General Medicine',
                   doctor_name: 'Dr. Test', token_number: 7, status: 'in_consultation' },
          patient: { id: 'PAT-9', name: 'Ravi Kumar', uhid: 'UH-0909', age: 61, sex: 'M',
                     portal_login: true, date_of_birth_on_file: true },
          admittable: true, refusal: null, note: null,
          carried: { chief_complaint: 'Breathless', initial_diagnosis: null },
          opd_vitals: [], medicines_listed: true, opd_medicines: [], allergies: [],
          allergy_status: 'none_recorded', readmission: true, readmission_note: NOTE,
        })
      }
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<AdmitPatientModal onClose={() => {}} onSuccess={() => {}}
      existing={{ visitId: 'OPD-1', noSignedNoteReason: 'On paper' }} />)
    expect(await screen.findByText(NOTE)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Readmission onto the same record' })).toBeInTheDocument()
  })
})
