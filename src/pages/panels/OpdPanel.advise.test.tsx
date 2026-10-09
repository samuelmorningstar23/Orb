/**
 * Advise admission on the OPD tab (opd-17a).
 *
 * Finish visit offers Advise admission. Choosing it finishes nothing on this
 * screen: it opens the admission form for this patient's own record with the
 * visit and, when the note is not signed, the reason (ruling OPD-N8), and the
 * server finishes the visit as admitted in the admission's own transaction.
 * After the admission is saved the record closes and the queue is read again.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => ({
  apiFetch: vi.fn(),
  modalProps: [] as Array<Record<string, unknown>>,
  /** The signed-in account's role: Finish visit offers admission only to a
   *  role holding patient.write. */
  role: 'doctor',
}))

vi.mock('../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => h.apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
  getUser: () => ({ id: 'DOC-1', name: 'Dr Iyer', role: h.role }),
  socket: { connected: true, on: () => {}, off: () => {}, emit: vi.fn() },
}))

vi.mock('../../components/AdmitPatientModal', () => ({
  default: (props: { existing?: { visitId: string; noSignedNoteReason?: string | null };
    onClose: () => void; onSuccess: () => void }) => {
    h.modalProps.push(props as unknown as Record<string, unknown>)
    return (
      <div role="dialog" aria-label="Admission form">
        <span>Admitting from visit {props.existing?.visitId}</span>
        <button type="button" onClick={props.onSuccess}>Pretend the admission was saved</button>
        <button type="button" onClick={props.onClose}>Close the form</button>
      </div>
    )
  },
}))

import OpdPanel from './OpdPanel'

const DEPT = 'Cardiology'

function visit(id: string, token: number, extra: Record<string, unknown> = {}) {
  return {
    id, patient_id: `P-${id}`, patient_name: `Patient ${id}`, patient_mrn: `UHID-${id}`,
    patient_age: 50, patient_sex: 'F', department: DEPT, doctor_id: 'DOC-1',
    doctor_name: 'Dr Iyer', visit_date: '2026-10-09', token_number: token, visit_type: 'new',
    chief_complaint: 'cough', status: 'in_consultation', registered_at: '2026-10-09T03:00:00+00:00',
    called_at: null, completed_at: null, cancelled_at: null, cancel_reason: null,
    consultation_charge_id: null, notes: null, waiting_minutes: 12, consultation_minutes: null,
    priority: 'normal', priority_label: 'Normal', priority_reason: null, room: null,
    call_count: 1, skipped: false, skipped_at: null, cancel_reason_code: null,
    cancel_reason_label: null, disposition: null, disposition_at: null,
    consultation_status: 'draft', ...extra,
  }
}

const QUEUE = {
  visit_date: '2026-10-09', department: DEPT, doctor_id: 'DOC-1',
  current: [visit('V-NOW', 4, { patient_name: 'Farah Now' })],
  waiting: [], skipped: [], unassigned: [], completed_count: 0, total: 1,
  priorities: [{ code: 'normal', label: 'Normal' }], poll_seconds: 30,
}

const RECORD = {
  patient: { id: 'P-V-NOW', name: 'Farah Now', uhid: 'UHID-V-NOW', age: 50,
    age_recorded_on: null, sex: 'F', preferred_language: null, record_status: 'registered',
    inpatient_now: false, date_of_birth_on_file: true },
  access: { arm: 'opd_visit', visit_id: 'V-NOW', visit_date: '2026-10-09', scope_until: '2026-10-16' },
  allergies: [], allergy_status: 'none_recorded', problems: [],
  opd_visits: [], results: [], consultations: [], consultations_available: true,
}

function noteView(signed: boolean) {
  return {
    visit_id: 'V-NOW', patient_id: 'P-V-NOW', visit_status: 'in_consultation',
    consultation: {
      id: 'OPC-1', visit_id: 'V-NOW', patient_id: 'P-V-NOW', department: DEPT, author_id: 'DOC-1',
      author_name: 'Dr Iyer', status: signed ? 'signed' : 'draft', chief_complaint: 'Cough',
      history: null, examination: null, vitals: null, diagnoses: [], plan: null, advice: null,
      follow_up_date: null, follow_up_note: null, referral_department: null,
      admission_advised: false, row_version: 2, created_at: '2026-10-09T04:00:00+00:00',
      updated_at: '2026-10-09T04:12:00+00:00', signed_by: signed ? 'DOC-1' : null,
      signed_by_name: signed ? 'Dr Iyer' : null, signed_at: signed ? '2026-10-09T04:20:00+00:00' : null,
      addenda: [],
    },
    allergy_review: { status: 'recorded', can_sign: true, charted_count: 0 },
    patient_admitted: false,
  }
}

function routeApi(signed: boolean) {
  h.apiFetch.mockImplementation(async (path: string) => {
    if (path === '/opd/my-queue') return QUEUE
    if (path === '/opd/meta') {
      return { feature: 'opd', enabled: true, settings: {}, dispositions: [{ code: 'home', label: 'Home' }] }
    }
    if (path.startsWith('/opd/patients/')) return RECORD
    if (path.endsWith('/consultation')) return noteView(signed)
    throw new Error(`unrouted ${path}`)
  })
}

function posted(): string[] {
  return h.apiFetch.mock.calls
    .filter(([, options]) => options && (options as RequestInit).method === 'POST')
    .map(([path]) => path as string)
}

async function openFinish() {
  render(<OpdPanel />)
  await screen.findAllByText('Farah Now')
  fireEvent.click(screen.getByRole('button', { name: /Open the record of Farah Now/ }))
  fireEvent.click(await screen.findByRole('button', { name: 'Finish visit' }))
  return screen.findByRole('dialog')
}

beforeEach(() => {
  h.apiFetch.mockReset()
  h.modalProps = []
  h.role = 'doctor'
})

afterEach(() => cleanup())

describe('Advise admission', () => {
  it('opens the admission form with the visit and the reason, finishing nothing here', async () => {
    routeApi(false)
    const dialog = await openFinish()
    expect(within(dialog).getByText('To admit the patient, choose Advise admission.')).toBeInTheDocument()
    fireEvent.change(within(dialog).getByLabelText('Outcome'), { target: { value: 'advise_admission' } })
    const go = within(dialog).getByRole('button', { name: 'Continue to admission' })
    expect(go).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText('Why is there no signed note?'),
      { target: { value: 'Note written on paper' } })
    expect(go).not.toBeDisabled()
    fireEvent.click(go)

    expect(await screen.findByRole('dialog', { name: 'Admission form' })).toHaveTextContent(
      'Admitting from visit V-NOW')
    expect(h.modalProps.at(-1)?.existing).toEqual({ visitId: 'V-NOW', noSignedNoteReason: 'Note written on paper' })
    expect(posted()).toEqual([])
  })

  it('sends no reason when the note is signed, and closes the record after the admission', async () => {
    routeApi(true)
    const dialog = await openFinish()
    fireEvent.change(within(dialog).getByLabelText('Outcome'), { target: { value: 'advise_admission' } })
    expect(within(dialog).queryByLabelText('Why is there no signed note?')).toBeNull()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Continue to admission' }))
    await screen.findByRole('dialog', { name: 'Admission form' })
    expect(h.modalProps.at(-1)?.existing).toEqual({ visitId: 'V-NOW', noSignedNoteReason: null })

    const reads = h.apiFetch.mock.calls.filter(([p]) => p === '/opd/my-queue').length
    fireEvent.click(screen.getByRole('button', { name: 'Pretend the admission was saved' }))
    expect(await screen.findByText(/Farah Now admitted from this visit/)).toBeInTheDocument()
    expect(screen.queryByRole('dialog', { name: 'Admission form' })).toBeNull()
    expect(screen.getByText('Choose a patient to open their outpatient record.')).toBeInTheDocument()
    await waitFor(() => expect(h.apiFetch.mock.calls.filter(([p]) => p === '/opd/my-queue').length)
      .toBeGreaterThan(reads))
  })

  it('is not offered to an account that cannot admit', async () => {
    /* No role holding opd.consult lacks patient.write today; if one ever
       does, it is told to send the patient to the admission desk instead. */
    h.role = 'cashier'
    routeApi(true)
    const dialog = await openFinish()
    const outcome = within(dialog).getByLabelText('Outcome')
    expect(within(outcome).queryByRole('option', { name: 'Advise admission' })).toBeNull()
    expect(within(dialog).getByText('To admit the patient, send them to the admission desk with your note.'))
      .toBeInTheDocument()
  })

  it('closing the form leaves the visit open', async () => {
    routeApi(true)
    const dialog = await openFinish()
    fireEvent.change(within(dialog).getByLabelText('Outcome'), { target: { value: 'advise_admission' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Continue to admission' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Close the form' }))
    expect(screen.queryByRole('dialog', { name: 'Admission form' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Finish visit' })).toBeInTheDocument()
    expect(posted()).toEqual([])
  })
})
