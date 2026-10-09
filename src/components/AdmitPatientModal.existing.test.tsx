/**
 * The admission form for a patient already on file (opd-17a): a registered
 * outpatient admitted from their clinic visit onto their own record.
 *
 * What it must never get wrong:
 *
 * 1. IDENTITY IS FIXED. The record's name, UHID, age and sex are shown and
 *    cannot be edited; no identity field and no date of birth is sent. The
 *    clinician types what the patient STATES (ruling OPD-S1), and the form
 *    cannot be reviewed until it is enough for the server's check.
 * 2. OPD OBSERVATIONS ARE FOR REFERENCE (OPD-S3): listed with their time,
 *    nothing to type, and no 'vitals' field is sent.
 * 3. OPD MEDICINES ARE FOR REFERENCE (OPD-S4): listed with their times and
 *    not sent anywhere; on a Ward Record computer none is listed.
 * 4. ONE KEY PER FORM, sent to the conversion route, with the visit and the
 *    finish-visit reason.
 * 5. THE SERVER'S WORDS. A patient who cannot be admitted from this visit is
 *    told why, and the form cannot be reviewed.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetClinicalSwitches, resetModuleFlags, setClinicalSwitches, setModuleFlags } from '../shellConfig'

const apiFetch = vi.hoisted(() => vi.fn())
vi.mock('../api/client', () => ({
  admitPatient: vi.fn(() => Promise.reject(new Error('the new-patient door must not be used'))),
  apiFetch: (...a: unknown[]) => apiFetch(...a),
}))
vi.mock('../App', () => ({
  useAuth: () => ({
    user: { id: 'D001', name: 'Dr. Test', role: 'doctor', department: 'General Medicine' },
  }),
}))

import AdmitPatientModal from './AdmitPatientModal'

const OPTIONS = {
  departments: ['General Medicine', 'General Surgery'],
  default_department: 'General Medicine',
  doctors: [{ id: 'D001', name: 'Dr. Test', department: 'General Medicine' }],
  wards: [{ ward: 'GM', departments: ['General Medicine'] }],
  beds: [],
}

/** Bed setup as /admissions/options sends it with ORB_ADT on (adt-5). */
const MASTER = {
  backtime_hours: 72,
  authorisers: [],
  wards: [{
    id: 'WRD-GM', code: 'GM-1', name: 'General ward one', department: 'General Medicine',
    ward_type: 'general', cleaning_tracked: true, register_complete: false,
    beds: [{
      bed_id: 'BED-7', bed_label: '7', unit_code: 'GM-1/7', room: null, bed_class: null,
      isolation: false, temporary: false, status: 'available', status_words: 'Available',
      tone: 'ready', markers: [], occupied: false, selectable: true, needs: null,
      terminal_clean: false,
    }],
  }],
}

let options: Record<string, unknown> = OPTIONS

const PREVIEW = {
  visit: { id: 'OPD-1', visit_date: '2026-10-09', department: 'General Medicine',
           doctor_name: 'Dr. Test', token_number: 7, status: 'in_consultation' },
  patient: { id: 'PAT-1', name: 'Meera Rao', uhid: 'UH-4411', age: 58, sex: 'F',
             portal_login: false, date_of_birth_on_file: true },
  admittable: true,
  refusal: null,
  note: { status: 'draft', signed: false, signed_at: null },
  carried: { chief_complaint: 'Cough and fever', initial_diagnosis: 'Community acquired pneumonia' },
  opd_vitals: [{ measured_at: '2026-10-09T04:30:00Z', measured_by_name: 'Dr. Test',
                 values: { heart_rate: 118, spo2: 91 } }],
  medicines_listed: true,
  opd_medicines: [{ drug_name: 'Amoxicillin', dose: '500 mg', route: 'Oral', frequency: 'TDS',
                    is_prn: false, status: 'prescribed', prescribed_at: '2026-10-09T04:40:00Z' }],
  allergies: [{ substance: 'penicillin', reaction: 'rash', severity: 'severe' }],
  allergy_status: 'recorded',
}

const ADMITTED = {
  success: true, replayed: false, patient_id: 'PAT-1', admission_id: 'ADM-X', channel_id: 'c1',
  mrn: 'UH-4411', patient_login_id: 'PT1', temporary_password: 'once-only', portal_login_created: true,
  date_of_birth_recorded: true, bed_warning: null,
}

let preview: Record<string, unknown> = PREVIEW
let admitReply: () => Promise<unknown> = () => Promise.resolve(ADMITTED)

function conversionCalls() {
  return apiFetch.mock.calls.filter(([path]) => path === '/admissions/admit-existing')
}

beforeEach(() => {
  resetModuleFlags()
  options = OPTIONS
  preview = PREVIEW
  admitReply = () => Promise.resolve(ADMITTED)
  apiFetch.mockReset()
  apiFetch.mockImplementation((path: string) => {
    if (path === '/admissions/options') return Promise.resolve(options)
    if (path === '/admissions/admit-existing/visits/OPD-1') return Promise.resolve(preview)
    if (path === '/admissions/admit-existing') return admitReply()
    return Promise.reject(new Error(`unexpected ${path}`))
  })
})

afterEach(() => {
  cleanup()
  resetModuleFlags()
  resetClinicalSwitches()
})

function renderExisting(reason: string | null = 'Note written on paper') {
  return render(
    <AdmitPatientModal onClose={() => {}} onSuccess={() => {}}
      existing={{ visitId: 'OPD-1', noSignedNoteReason: reason }} />,
  )
}

function stateIdentity() {
  fireEvent.change(screen.getByLabelText(/Name, as the patient says it/), { target: { value: 'Meera Rao' } })
  fireEvent.change(screen.getByLabelText(/^Date of birth/), { target: { value: '1968-03-14' } })
  fireEvent.change(screen.getByLabelText(/^Last 4 digits/), { target: { value: '1234' } })
}

async function openAdmissionAndPickWard(container: HTMLElement) {
  fireEvent.click(screen.getByRole('button', { name: /^Admission/ }))
  await waitFor(() => expect(screen.getByLabelText(/^Attending Doctor/)).toHaveValue('D001'))
  fireEvent.change(container.querySelector('#admit-ward') as HTMLInputElement, { target: { value: 'GM' } })
}

describe('AdmitPatientModal for a patient already on file', () => {
  it('shows the record read-only and asks only what the patient states', async () => {
    renderExisting()
    expect(await screen.findByRole('heading', { name: 'Meera Rao' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Admit from outpatients' })).toBeInTheDocument()
    expect(screen.getByText(/58 y, Female, UHID UH-4411/)).toBeInTheDocument()
    expect(screen.getByText(/General Medicine clinic, token 7/)).toBeInTheDocument()
    expect(screen.queryByText(/Full Name/)).toBeNull()
    expect((screen.getByLabelText(/Name, as the patient says it/) as HTMLInputElement).value).toBe('')
    expect(document.body.innerHTML).not.toContain('1968')
    expect(screen.getByRole('button', { name: 'Review Admission' })).toBeDisabled()
  })

  it('starts from the consultation and lists the clinic observations for reference only', async () => {
    const { container } = renderExisting()
    await screen.findByRole('heading', { name: 'Meera Rao' })
    fireEvent.click(screen.getByRole('button', { name: /^Clinical/ }))
    expect((container.querySelector('textarea.admit-input') as HTMLTextAreaElement).value).toBe('Cough and fever')
    fireEvent.click(screen.getByRole('button', { name: /^Observations/ }))
    expect(screen.getByText('Observations from the clinic, for reference')).toBeInTheDocument()
    expect(screen.getByText(/Pulse 118 per min, SpO2 91%/)).toBeInTheDocument()
    expect(screen.getByText(/not copied to the observation chart/)).toBeInTheDocument()
    expect(screen.queryByText('Heart Rate (bpm)')).toBeNull()
    expect(container.querySelectorAll('.admit-modal__section input').length).toBe(0)
  })

  it('lists the charted allergies and the outpatient medicines, never carrying them', async () => {
    renderExisting()
    await screen.findByRole('heading', { name: 'Meera Rao' })
    fireEvent.click(screen.getByRole('button', { name: /^History/ }))
    expect(screen.getByText('Allergies already on the chart')).toBeInTheDocument()
    expect(screen.getByText(/penicillin: rash \(severe\)/)).toBeInTheDocument()
    expect(screen.getByText('Outpatient medicines, for reference')).toBeInTheDocument()
    expect(screen.getByText(/Amoxicillin 500 mg Oral TDS/)).toBeInTheDocument()
    expect(screen.getByText(/None of these is carried onto the inpatient medication chart/)).toBeInTheDocument()
  })

  it('lists no medicine on a Ward Record computer', async () => {
    act(() => { setClinicalSwitches({ ward_record: true, scoring_enabled: false, models_enabled: false }) })
    renderExisting()
    await screen.findByRole('heading', { name: 'Meera Rao' })
    fireEvent.click(screen.getByRole('button', { name: /^History/ }))
    expect(screen.queryByText('Outpatient medicines, for reference')).toBeNull()
    expect(screen.queryByText(/Amoxicillin/)).toBeNull()
  })

  it('sends the visit, what the patient stated and one key, and no identity or observations', async () => {
    const { container } = renderExisting()
    await screen.findByRole('heading', { name: 'Meera Rao' })
    stateIdentity()
    await openAdmissionAndPickWard(container)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Review Admission' })) })
    expect(screen.getByText('Patient on file')).toBeInTheDocument()
    expect(screen.getByText('Checked against the record when you admit')).toBeInTheDocument()

    let attempt = 0
    admitReply = () => {
      attempt += 1
      return attempt === 1 ? Promise.reject(new Error('network down')) : Promise.resolve(ADMITTED)
    }
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm Admission' })) })
    expect(await screen.findByText('network down')).toBeInTheDocument()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm Admission' })) })

    const calls = conversionCalls()
    expect(calls).toHaveLength(2)
    const keys = calls.map(([, init]) => ((init as RequestInit).headers as Record<string, string>)['Idempotency-Key'])
    expect(keys[0]).toMatch(/\S{8,}/)
    expect(keys[1]).toBe(keys[0])
    const body = JSON.parse(String((calls[1][1] as RequestInit).body))
    expect(body).toMatchObject({
      opd_visit_id: 'OPD-1',
      identity_check: { name: 'Meera Rao', date_of_birth: '1968-03-14', phone_last4: '1234' },
      no_signed_note_reason: 'Note written on paper',
      admission_source: 'clinic',
      department: 'General Medicine',
      ward: 'GM',
      chief_complaint: 'Cough and fever',
      initial_diagnosis: 'Community acquired pneumonia',
    })
    for (const field of ['name', 'age', 'sex', 'date_of_birth', 'phone', 'mrn', 'vitals', 'address']) {
      expect(body).not.toHaveProperty(field)
    }
    expect(await screen.findByText('once-only')).toBeInTheDocument()
  })

  it('places the patient with the bed picker when beds and admissions is on', async () => {
    setModuleFlags({ adt: true })
    options = { ...OPTIONS, master: MASTER }
    const { container } = renderExisting()
    await screen.findByRole('heading', { name: 'Meera Rao' })
    stateIdentity()
    fireEvent.click(screen.getByRole('button', { name: /^Admission/ }))
    await waitFor(() => expect(screen.getByLabelText(/^Attending Doctor/)).toHaveValue('D001'))
    expect(screen.getByText('Ward and bed')).toBeInTheDocument()
    expect(container.querySelector('#admit-ward')).toBeNull()
    fireEvent.change(screen.getByLabelText('Ward'), { target: { value: 'WRD-GM' } })
    fireEvent.click(screen.getByRole('radio', { name: /Bed 7/ }))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Review Admission' })) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Confirm Admission' })) })

    expect(apiFetch.mock.calls.filter(([path]) => path === '/admissions/admit')).toHaveLength(0)
    const calls = conversionCalls()
    expect(calls).toHaveLength(1)
    const body = JSON.parse(String((calls[0][1] as RequestInit).body))
    expect(body).toMatchObject({ opd_visit_id: 'OPD-1', ward: 'GM-1', bed: '7',
                                 ward_id: 'WRD-GM', bed_id: 'BED-7' })
    expect(body).not.toHaveProperty('vitals')
    expect(body).not.toHaveProperty('name')
  })

  it('says why the patient cannot be admitted from this visit and cannot be reviewed', async () => {
    // The server's refusal is the visit and why, nothing about the patient.
    preview = {
      visit: { ...PREVIEW.visit, status: 'waiting' },
      admittable: false,
      refusal: { error: 'patient_already_admitted',
                 message: 'This patient is already admitted. Open their inpatient chart.' },
    }
    renderExisting()
    expect(await screen.findByRole('alert')).toHaveTextContent('This patient is already admitted.')
    expect(screen.getByText(/General Medicine clinic, token 7/)).toBeInTheDocument()
    expect(screen.getByText('Cannot be admitted from this visit')).toBeInTheDocument()
    expect(screen.queryByText(/Loading the record/)).toBeNull()
    expect(screen.queryByLabelText(/Name, as the patient says it/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Review Admission' })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: /^History/ }))
    expect(screen.queryByText('Allergies already on the chart')).toBeNull()
    expect(screen.queryByText('Outpatient medicines, for reference')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /^Observations/ }))
    expect(screen.getByRole('alert')).toHaveTextContent('This patient is already admitted.')
    expect(screen.queryByText('Observations from the clinic, for reference')).toBeNull()
    expect(screen.queryByText(/Loading the patient/)).toBeNull()
  })

  it('says when the record cannot be read', async () => {
    apiFetch.mockImplementation((path: string) => {
      if (path === '/admissions/options') return Promise.resolve(OPTIONS)
      return Promise.reject(new Error('Outpatient visit not found.'))
    })
    renderExisting()
    expect(await screen.findByRole('alert')).toHaveTextContent('Outpatient visit not found.')
  })
})
