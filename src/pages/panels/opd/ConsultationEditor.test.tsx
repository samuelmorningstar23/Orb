/**
 * The OPD consultation editor, its diagnosis picker, the visit's
 * investigations and the consultation print (opd-11).
 *
 *  1. SAVE sends only what changed, against the version the editor read; a
 *     new note sends no version; a stale version says so and offers a reload.
 *  2. SIGN needs the allergy review (ruling OPD-S7): without one the button is
 *     off and the panel offers the review; with one, unsaved changes are saved
 *     first and the note is then signed and locked (no edit form, addenda
 *     only).
 *  3. VITALS are plain values with their time: no colour, no band, no score
 *     (rulings OPD-S3, OPD-S6).
 *  4. DIAGNOSES: a code from the search or the words as written.
 *  5. INVESTIGATIONS: a laboratory test or imaging is ordered on the visit
 *     with its code and priority only; a cancel needs a reason.
 *  6. THE PRINT carries the doctor's name and registration number and only
 *     what the record holds: no invented text, no date of birth or phone.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ConsultationView, OpdConsultation, VisitPrint } from '../../../api/opd'
import OpdConsultationPrint from '../../../components/print/OpdConsultationPrint'
import { formatClinicalDateTime } from '../../../lib/datetime'
import ConsultationEditor from './ConsultationEditor'

const h = vi.hoisted(() => ({ apiFetch: vi.fn() }))

vi.mock('../../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => h.apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
  // The signed-in doctor: Finish visit asks the role whether it may admit.
  getUser: () => ({ id: 'DOC-1', name: 'Dr Iyer', role: 'doctor' }),
  socket: { connected: true, on: () => {}, off: () => {}, emit: vi.fn() },
}))

const MEASURED = '2026-10-09T04:10:00+00:00'

function note(extra: Partial<OpdConsultation> = {}): OpdConsultation {
  return {
    id: 'OPC-1', visit_id: 'V-1', patient_id: 'P-1', department: 'Medicine', author_id: 'DOC-1',
    author_name: 'Dr Iyer', status: 'draft', chief_complaint: 'Cough', history: null,
    examination: null,
    vitals: { values: { heart_rate: 88, systolic_bp: 142, diastolic_bp: 90, spo2: 89, temperature: 38.9 },
      measured_at: MEASURED, measured_by: 'DOC-1', measured_by_name: 'Dr Iyer' },
    diagnoses: [], plan: null, advice: null, follow_up_date: null, follow_up_note: null,
    referral_department: null, admission_advised: false, row_version: 2,
    created_at: '2026-10-09T04:00:00+00:00', updated_at: '2026-10-09T04:12:00+00:00',
    signed_by: null, signed_by_name: null, signed_at: null, addenda: [],
    ...extra,
  }
}

function view(extra: Partial<ConsultationView> & { review?: Record<string, unknown> } = {}): ConsultationView {
  const { review, ...rest } = extra
  return {
    visit_id: 'V-1', patient_id: 'P-1', visit_status: 'in_consultation',
    consultation: note(),
    allergy_review: (review ?? { status: 'not_reviewed', can_sign: false, charted_count: 0,
      reviewed_this_visit: false, basis: null, assessment: null }) as ConsultationView['allergy_review'],
    patient_admitted: false,
    ...rest,
  }
}

const REVIEWED = { status: 'none_known', can_sign: true, charted_count: 0, reviewed_this_visit: true,
  basis: 'this_visit', assessment: { status: 'none_known', status_label: 'No known allergies',
    assessed_at: '2026-10-09T04:05:00+00:00' } }

type Handler = (options?: RequestInit) => unknown
function routeApi(overrides: Record<string, Handler> = {}) {
  h.apiFetch.mockImplementation(async (path: string, options?: RequestInit) => {
    for (const [prefix, fn] of Object.entries(overrides)) {
      if (path.startsWith(prefix)) return fn(options)
    }
    if (path === '/opd/visits/V-1/orders') return { visit_id: 'V-1', orders: [] }
    if (path.startsWith('/opd/terminology/search')) return { kind: 'diagnosis', results: [], truncated: false, min_chars: 2 }
    throw new Error(`unrouted ${path}`)
  })
}

function calls(prefix: string, method = 'GET') {
  return h.apiFetch.mock.calls.filter(([p, o]) => String(p).startsWith(prefix)
    && ((o as RequestInit | undefined)?.method ?? 'GET') === method)
}

function bodyOf(call: unknown[]): Record<string, unknown> {
  return JSON.parse(String((call[1] as RequestInit).body))
}

/** The editor inside a parent that keeps the note, as OpdPanel does. */
function Harness({ initial, onReview }: { initial: ConsultationView; onReview?: () => void }) {
  const [current, setCurrent] = useState(initial)
  return (
    <ConsultationEditor visitId="V-1" patientName="Chitra Second" view={current}
      onChanged={setCurrent} onReviewAllergies={onReview} />
  )
}

let print: ReturnType<typeof vi.spyOn>
beforeEach(() => {
  h.apiFetch.mockReset()
  print = vi.spyOn(window, 'print').mockImplementation(() => {})
})
afterEach(() => {
  cleanup()
  print.mockRestore()
})

describe('saving the draft', () => {
  it('sends only what changed, with the version the editor read', async () => {
    routeApi({ '/opd/visits/V-1/consultation': options => ({
      ...note(), ...JSON.parse(String(options?.body)), row_version: 3 }) })
    render(<Harness initial={view()} />)
    const save = screen.getByRole('button', { name: 'Save draft' })
    expect(save).toBeDisabled()
    fireEvent.change(screen.getByLabelText('History'), { target: { value: 'Three days, dry' } })
    fireEvent.change(screen.getByLabelText('Plan'), { target: { value: 'Review in five days' } })
    fireEvent.click(save)
    await waitFor(() => expect(calls('/opd/visits/V-1/consultation', 'PUT')).toHaveLength(1))
    const sent = bodyOf(calls('/opd/visits/V-1/consultation', 'PUT')[0])
    expect(sent).toEqual({ row_version: 2, history: 'Three days, dry', plan: 'Review in five days' })
    expect(await screen.findByText('Saved, version 3.')).toBeInTheDocument()
  })

  it('a first save sends no version, and typed vitals carry their time', async () => {
    routeApi({ '/opd/visits/V-1/consultation': options => ({
      ...note({ vitals: null }), ...JSON.parse(String(options?.body)), row_version: 1, vitals: null }) })
    render(<Harness initial={view({ consultation: null })} />)
    expect(screen.getByText('No note has been started for this visit.')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Complaint'), { target: { value: 'Fever' } })
    fireEvent.change(screen.getByLabelText('Pulse (per min)'), { target: { value: '104' } })
    fireEvent.change(screen.getByLabelText('Measured at'), { target: { value: '2026-10-09T09:30' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    await waitFor(() => expect(calls('/opd/visits/V-1/consultation', 'PUT')).toHaveLength(1))
    const sent = bodyOf(calls('/opd/visits/V-1/consultation', 'PUT')[0])
    expect(sent.row_version).toBeNull()
    expect(sent.chief_complaint).toBe('Fever')
    expect(sent.vitals).toEqual({ values: { heart_rate: 104 },
      measured_at: new Date('2026-10-09T09:30').toISOString() })
  })

  it('refuses a vital outside its range before sending anything', async () => {
    routeApi()
    render(<Harness initial={view()} />)
    fireEvent.change(screen.getByLabelText('SpO2 (%)'), { target: { value: '140' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('SpO2 140 is outside 1 to 100.')
    expect(calls('/opd/visits/V-1/consultation', 'PUT')).toHaveLength(0)
  })

  it('a stale version says so and the reload brings the colleague\'s note in', async () => {
    routeApi({
      '/opd/visits/V-1/consultation': options => {
        if (options?.method === 'PUT') {
          throw Object.assign(new Error('stale'), { status: 409,
            detail: { error: 'stale_version', current_version: 3 } })
        }
        return view({ consultation: note({ row_version: 3, plan: 'Colleague plan' }) })
      },
    })
    render(<Harness initial={view()} />)
    fireEvent.change(screen.getByLabelText('Plan'), { target: { value: 'My plan' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/Someone saved this note after you opened it/)
    expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Reload the note' }))
    await waitFor(() => expect(screen.getByLabelText('Plan')).toHaveValue('Colleague plan'))
    expect(screen.getByText(/Draft, version 3/)).toBeInTheDocument()
  })
})

describe('signing and the lock', () => {
  it('cannot sign before an allergy review and offers the review', async () => {
    routeApi()
    const onReview = vi.fn()
    render(<Harness initial={view()} onReview={onReview} />)
    expect(screen.getByRole('button', { name: 'Sign the note' })).toBeDisabled()
    expect(screen.getByText(/A note can be signed once the patient.s allergies are recorded/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Record the review now' }))
    expect(onReview).toHaveBeenCalledTimes(1)
  })

  it('saves unsaved changes first, signs, then locks the note', async () => {
    routeApi({
      '/opd/visits/V-1/consultation/sign': options => note({
        row_version: Number(bodyOf(['', options]).row_version) + 1, status: 'signed',
        plan: 'Chest X-ray', signed_by: 'DOC-1', signed_by_name: 'Dr Iyer',
        signed_at: '2026-10-09T04:30:00+00:00' }),
      '/opd/visits/V-1/consultation': options => note({
        ...JSON.parse(String(options?.body)), row_version: 3 }),
    })
    render(<Harness initial={view({ review: REVIEWED })} />)
    expect(screen.getByText('Allergy review recorded on this visit.')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Plan'), { target: { value: 'Chest X-ray' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sign the note' }))
    expect(await screen.findByText(/Signed by Dr Iyer on/)).toBeInTheDocument()
    const order = h.apiFetch.mock.calls.map(([p, o]) => `${(o as RequestInit | undefined)?.method ?? 'GET'} ${p}`)
      .filter(s => !s.includes('/orders') && !s.includes('/terminology'))
    expect(order).toEqual(['PUT /opd/visits/V-1/consultation', 'POST /opd/visits/V-1/consultation/sign'])
    expect(bodyOf(calls('/opd/visits/V-1/consultation/sign', 'POST')[0])).toEqual({ row_version: 3 })
    // Locked: no edit form, no save or sign; addenda only.
    expect(screen.queryByRole('form', { name: 'Edit the note' })).toBeNull()
    expect(screen.queryByLabelText('Plan')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Save draft' })).toBeNull()
    expect(screen.getByLabelText('Add an addendum')).toBeInTheDocument()
    expect(screen.getByText('Chest X-ray', { exact: false })).toBeInTheDocument()
  })

  it('an allergy review refusal from the server is said in plain words', async () => {
    routeApi({
      '/opd/visits/V-1/consultation/sign': () => {
        throw Object.assign(new Error('refused'), { status: 409,
          detail: { error: 'allergy_review_required' } })
      },
    })
    render(<Harness initial={view({ review: REVIEWED })} />)
    fireEvent.click(screen.getByRole('button', { name: 'Sign the note' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/Record the allergy review before signing/)
  })

  it('an admitted patient\'s note is read only', () => {
    routeApi()
    render(<Harness initial={view({ patient_admitted: true })} />)
    expect(screen.getByText('This patient is now admitted. The note is read only here.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save draft' })).toBeNull()
  })
})

describe('vitals are plain values', () => {
  it('shows the measured values with their time, with no colour, band or score', () => {
    routeApi()
    const { container } = render(<Harness initial={view()} />)
    expect(screen.getByText(
      `Measured ${formatClinicalDateTime(MEASURED)} by Dr Iyer: Pulse 88 per min, BP 142/90 mmHg, `
      + 'SpO2 89%, Temperature 38.9 °C')).toBeInTheDocument()
    expect(screen.getByLabelText('SpO2 (%)')).toHaveValue(89)
    const text = container.textContent ?? ''
    for (const word of ['NEWS2', 'News2', 'score', 'band', 'critical', 'abnormal', 'high', 'low',
      'normal', 'warning']) {
      expect(text).not.toMatch(new RegExp(`\\b${word}\\b`, 'i'))
    }
    for (const el of Array.from(container.querySelectorAll('*'))) {
      expect((el as HTMLElement).style.color, el.outerHTML.slice(0, 80)).toBe('')
      expect((el as HTMLElement).style.background, el.outerHTML.slice(0, 80)).toBe('')
      expect(el.className.toString()).not.toMatch(/status|band|risk|news|alert|danger|warn/)
    }
  })
})

describe('diagnoses', () => {
  it('adds a code from the search or the words as written', async () => {
    routeApi({
      '/opd/terminology/search': () => ({ kind: 'diagnosis', truncated: false, min_chars: 2,
        results: [{ code_system: 'ICD10', code: 'I10', display: 'Essential (primary) hypertension' }] }),
      '/opd/visits/V-1/consultation': options => note({ ...JSON.parse(String(options?.body)), row_version: 3 }),
    })
    render(<Harness initial={view()} />)
    const find = screen.getByLabelText('Add a diagnosis')
    fireEvent.change(find, { target: { value: 'hyper' } })
    fireEvent.click(await screen.findByRole('button', { name: /Essential \(primary\) hypertension/ }))
    const searched = calls('/opd/terminology/search')
    expect(String(searched[searched.length - 1][0])).toContain('kind=diagnosis')
    fireEvent.change(screen.getByLabelText('Add a diagnosis'), { target: { value: 'Viral cough' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add as written, no code' }))
    const boxes = screen.getAllByRole('checkbox', { name: 'Add to the problem list when signed' })
    expect(boxes[0]).toBeEnabled()
    expect(boxes[1]).toBeDisabled()
    fireEvent.click(boxes[0])
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    await waitFor(() => expect(calls('/opd/visits/V-1/consultation', 'PUT')).toHaveLength(1))
    expect(bodyOf(calls('/opd/visits/V-1/consultation', 'PUT')[0]).diagnoses).toEqual([
      { text: 'Essential (primary) hypertension', code: 'I10', code_system: 'ICD10', add_to_problems: true },
      { text: 'Viral cough', add_to_problems: false },
    ])
  })
})

describe('investigations on the visit', () => {
  it('orders a test with its code and priority only, and cancels with a reason', async () => {
    const ordered = { id: 'ORD-1', opd_visit_id: 'V-1', order_type: 'lab', code_system: 'LOINC',
      code: '58410-2', description: 'CBC panel - Blood by Automated count', priority: 'urgent',
      status: 'active', result_text: null, result_at: null, ordered_by_name: 'Dr Iyer',
      created_at: '2026-10-09T04:40:00+00:00' }
    routeApi({
      '/opd/terminology/search': () => ({ kind: 'investigation', truncated: false, min_chars: 2,
        results: [{ code_system: 'LOINC', code: '58410-2', display: 'CBC panel - Blood by Automated count',
          order_type: 'lab' }] }),
      '/opd/visits/V-1/orders/ORD-1/cancel': () => ({ ...ordered, status: 'cancelled' }),
      '/opd/visits/V-1/orders': options => (options?.method === 'POST' ? ordered
        : { visit_id: 'V-1', orders: [] }),
    })
    render(<Harness initial={view()} />)
    const panel = await screen.findByRole('region', { name: 'Investigations on this visit' })
    expect(await within(panel).findByText('None ordered.')).toBeInTheDocument()
    fireEvent.change(within(panel).getByLabelText('Order a test'), { target: { value: 'cbc' } })
    fireEvent.change(within(panel).getByLabelText('Priority'), { target: { value: 'urgent' } })
    fireEvent.click(await within(panel).findByRole('button', { name: 'Order CBC panel - Blood by Automated count' }))
    await waitFor(() => expect(calls('/opd/visits/V-1/orders', 'POST')).toHaveLength(1))
    expect(bodyOf(calls('/opd/visits/V-1/orders', 'POST')[0])).toEqual(
      { code_system: 'LOINC', code: '58410-2', priority: 'urgent' })
    const searched = calls('/opd/terminology/search')
    expect(String(searched[searched.length - 1][0])).toContain('kind=investigation')
    expect(await within(panel).findByText(/Laboratory · Urgent · Ordered/)).toBeInTheDocument()

    fireEvent.click(within(panel).getByRole('button', { name: 'Cancel CBC panel - Blood by Automated count' }))
    const confirm = within(panel).getByRole('button', { name: 'Cancel the order' })
    expect(confirm).toBeDisabled()
    fireEvent.change(within(panel).getByLabelText(/Why cancel/), { target: { value: 'Ordered in error' } })
    fireEvent.click(confirm)
    await waitFor(() => expect(calls('/opd/visits/V-1/orders/ORD-1/cancel', 'POST')).toHaveLength(1))
    expect(bodyOf(calls('/opd/visits/V-1/orders/ORD-1/cancel', 'POST')[0])).toEqual({ reason: 'Ordered in error' })
    expect(await within(panel).findByText(/Cancelled/)).toBeInTheDocument()
  })

  it('offers no new order on a finished visit, and no order sets anywhere', async () => {
    routeApi()
    render(<Harness initial={view({ visit_status: 'completed' })} />)
    const panel = await screen.findByRole('region', { name: 'Investigations on this visit' })
    expect(within(panel).queryByLabelText('Order a test')).toBeNull()
    expect(screen.queryByText(/order set/i)).toBeNull()
  })
})

describe('the consultation print', () => {
  const PRINT: VisitPrint = {
    kind: 'consultation',
    printed_at: '2026-10-09T05:00:00+00:00',
    visit: { id: 'V-1', visit_date: '2026-10-09', department: 'Medicine', token_number: 4 },
    patient: { name: 'Chitra Second', uhid: 'UH-77', age: 52, sex: 'F' },
    consultation: {
      id: 'OPC-1', chief_complaint: 'Cough for a week', history: null, examination: null,
      vitals: { values: { heart_rate: 88, spo2: 95 }, measured_at: MEASURED },
      diagnoses: [{ text: 'Essential (primary) hypertension', code_system: 'ICD10', code: 'I10' }],
      plan: 'Chest X-ray', advice: null, follow_up_date: '2026-10-14', follow_up_note: null,
      referral_department: null, signed_at: '2026-10-09T04:30:00+00:00', addenda: [],
    },
    investigations: [{ description: 'Chest X-ray, portable (Orb local code)', order_type: 'imaging',
      priority: 'routine', status: 'active', created_at: '2026-10-09T04:20:00+00:00',
      ordered_by_name: 'Dr Iyer' }],
    doctors: [{ name: 'Dr Iyer', registration_number: 'KMC-4821' }],
  }

  it('prints the doctor\'s registration number and only what the record holds', () => {
    render(<OpdConsultationPrint print={PRINT} onAfterPrint={() => {}} autoPrint={false} />)
    const sheet = screen.getByTestId('opd-consultation-print')
    expect(within(sheet).getByText('Registration number KMC-4821')).toBeInTheDocument()
    expect(within(sheet).getByText('Dr Iyer')).toBeInTheDocument()
    expect(within(sheet).getByText('Cough for a week')).toBeInTheDocument()
    expect(within(sheet).getByText(/Pulse 88 per min, SpO2 95%/)).toBeInTheDocument()
    expect(within(sheet).getByText('Essential (primary) hypertension (ICD10 I10)')).toBeInTheDocument()
    expect(within(sheet).getByText('Chest X-ray, portable (Orb local code)')).toBeInTheDocument()
    // Sections the note leaves empty are not printed at all.
    for (const label of ['History', 'Examination', 'Advice', 'Referred to', 'Addenda']) {
      expect(within(sheet).queryByText(label)).toBeNull()
    }
    // The only words on the sheet are its labels and the record's own values.
    const labels = ['Outpatient consultation', 'Medicine', 'Token 4', 'Printed', 'Name', 'UHID',
      'Age and sex', 'Complaint', 'Vitals', 'Measured', 'Diagnosis', 'Plan', 'Follow-up',
      'Investigations ordered', 'Test', 'Kind', 'Priority', 'Ordered', 'Imaging', 'Routine',
      'Signed', 'Registration number', 'Female']
    let rest = sheet.textContent ?? ''
    const values = ['Chitra Second', 'UH-77', '52 y', 'Cough for a week', 'Pulse 88 per min', 'SpO2 95%',
      'Essential (primary) hypertension', 'ICD10 I10', 'Chest X-ray, portable (Orb local code)', 'Chest X-ray',
      'Dr Iyer', 'KMC-4821', '14 Oct 2026', '9 Oct 2026', formatClinicalDateTime(MEASURED),
      formatClinicalDateTime('2026-10-09T04:30:00+00:00'), formatClinicalDateTime('2026-10-09T04:20:00+00:00'),
      formatClinicalDateTime('2026-10-09T05:00:00+00:00')]
    for (const piece of [...values, ...labels].sort((a, b) => b.length - a.length)) {
      rest = rest.split(piece).join(' ')
    }
    expect(rest.replace(/[\s·,:().]+/g, '')).toBe('')
    const text = sheet.textContent ?? ''
    for (const word of ['NEWS2', 'score', 'band', 'Allerg', 'Date of birth', 'Phone', 'Address']) {
      expect(text).not.toContain(word)
    }
  })

  it('says when the registration number is not on file, never inventing one', () => {
    render(<OpdConsultationPrint print={{ ...PRINT, doctors: [{ name: 'Dr Iyer', registration_number: null }] }}
      onAfterPrint={() => {}} autoPrint={false} />)
    expect(screen.getByText('Registration number not on file')).toBeInTheDocument()
  })

  it('a signed note prints from the server\'s print answer', async () => {
    routeApi({ '/opd/visits/V-1/print': () => PRINT })
    render(<Harness initial={view({ consultation: note({ status: 'signed', signed_by: 'DOC-1',
      signed_by_name: 'Dr Iyer', signed_at: '2026-10-09T04:30:00+00:00' }) })} />)
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Print consultation' }))
    })
    expect(calls('/opd/visits/V-1/print')[0][0]).toBe('/opd/visits/V-1/print?kind=consultation')
    const sheet = await screen.findByTestId('opd-consultation-print')
    expect(sheet.parentElement).toBe(document.body)
    expect(within(sheet).getByText('Registration number KMC-4821')).toBeInTheDocument()
  })

  it('a draft offers no print', () => {
    routeApi()
    render(<Harness initial={view()} />)
    expect(screen.queryByRole('button', { name: 'Print consultation' })).toBeNull()
  })
})

describe('on the Outpatients tab', () => {
  function queueVisit(extra: Record<string, unknown> = {}) {
    return {
      id: 'V-1', patient_id: 'P-1', patient_name: 'Chitra Second', patient_mrn: 'UH-77',
      patient_age: 52, patient_sex: 'F', department: 'Medicine', doctor_id: 'DOC-1',
      doctor_name: 'Dr Iyer', visit_date: '2026-10-09', token_number: 4, visit_type: 'new',
      chief_complaint: 'cough', status: 'in_consultation', registered_at: '2026-10-09T03:00:00+00:00',
      called_at: null, completed_at: null, cancelled_at: null, cancel_reason: null,
      consultation_charge_id: null, notes: null, waiting_minutes: 12, consultation_minutes: null,
      priority: 'normal', priority_label: 'Normal', priority_reason: null, room: null,
      call_count: 1, skipped: false, skipped_at: null, cancel_reason_code: null,
      cancel_reason_label: null, disposition: null, disposition_at: null, consultation_status: 'draft',
      ...extra,
    }
  }
  const QUEUE = {
    visit_date: '2026-10-09', department: 'Medicine', doctor_id: 'DOC-1',
    current: [queueVisit()], waiting: [], skipped: [], unassigned: [], completed_count: 0, total: 1,
    priorities: [{ code: 'normal', label: 'Normal' }], poll_seconds: 30,
  }
  const RECORD = {
    patient: { id: 'P-1', name: 'Chitra Second', uhid: 'UH-77', age: 52, age_recorded_on: null, sex: 'F',
      preferred_language: null, record_status: 'registered', inpatient_now: false,
      date_of_birth_on_file: true },
    access: { arm: 'opd_visit', visit_id: 'V-1', visit_date: '2026-10-09', scope_until: '2026-10-16' },
    allergies: [], allergy_status: 'none_recorded', problems: [], opd_visits: [], results: [],
    consultations: [], consultations_available: true, allergy_assessment: null,
  }
  const META = { feature: 'opd', enabled: true,
    settings: { scope_days: 7, charge_at: 'completion', queue_poll_seconds: 30 },
    dispositions: [{ code: 'home', label: 'Home' }, { code: 'follow_up', label: 'Follow-up visit' },
      { code: 'referred_internal', label: 'Referred to another clinic' }] }

  async function openPanel(noteView: ConsultationView) {
    const { default: OpdPanel } = await import('../OpdPanel')
    routeApi({
      '/opd/my-queue': () => QUEUE,
      '/opd/meta': () => META,
      '/opd/patients/P-1/record': () => RECORD,
      '/opd/visits/V-1/consultation': () => noteView,
      '/opd/visits/V-1/allergy-review': () => ({ visit_id: 'V-1', patient_id: 'P-1', charted: [],
        reactivated: [], assessment: { status: 'none_known', status_label: 'No known allergies',
          assessed_at: '2026-10-09T04:50:00+00:00' }, allergy_review: REVIEWED }),
    })
    render(<OpdPanel />)
    fireEvent.click(await screen.findByRole('button', { name: /Open the record of Chitra Second/ }))
    return screen.findByRole('group', { name: 'Consultation note for Chitra Second' })
  }

  it('an allergy review recorded mid-note keeps the words not yet saved', async () => {
    const editor = await openPanel(view())
    fireEvent.change(within(editor).getByLabelText('Plan'), { target: { value: 'Unsaved plan' } })
    const allergies = screen.getByRole('region', { name: 'Allergies' })
    fireEvent.click(within(allergies).getByRole('button', { name: 'Record allergy review' }))
    const dialog = screen.getByRole('dialog', { name: 'Allergy review for Chitra Second' })
    fireEvent.click(within(dialog).getByLabelText('No known allergies'))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save allergy review' }))
    expect(await screen.findByText(/Allergy review saved for Chitra Second/)).toBeInTheDocument()
    await waitFor(() => expect(calls('/opd/patients/P-1/record').length).toBeGreaterThan(1))
    expect(screen.getByLabelText('Plan')).toHaveValue('Unsaved plan')
  })

  it('finishing says what the note says about follow-up and referral, and offers admission', async () => {
    await openPanel(view({ consultation: note({ follow_up_date: '2026-10-14',
      referral_department: 'Cardiology' }) }))
    fireEvent.click(screen.getByRole('button', { name: 'Finish visit' }))
    const dialog = await screen.findByRole('dialog')
    const outcome = within(dialog).getByLabelText('Outcome')
    /* opd-17a: Advise admission opens the admission form for this patient's
       own record (OpdPanel.advise.test.tsx). */
    const admit = within(outcome).getByRole('option', { name: 'Advise admission' })
    expect(admit).not.toBeDisabled()
    expect(within(dialog).getByText('To admit the patient, choose Advise admission.')).toBeInTheDocument()
    fireEvent.change(outcome, { target: { value: 'follow_up' } })
    expect(within(dialog).getByText(/Follow-up on 14 Oct 2026, from the note/)).toBeInTheDocument()
    fireEvent.change(outcome, { target: { value: 'referred_internal' } })
    expect(within(dialog).getByText('Referred to Cardiology, from the note.')).toBeInTheDocument()
  })
})
