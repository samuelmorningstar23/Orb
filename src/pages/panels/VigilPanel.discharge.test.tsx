/**
 * The chart's record, discharge and note paths print and show RECORDED data.
 *
 * Found 2026-10-01 and pinned here:
 *   - PRINT RECORD printed a "DISCHARGE SUMMARY" dated today, with standing
 *     clinical prose, for a patient still admitted. It now prints the
 *     inpatient record.
 *   - DISCHARGE was offered to nurses and administrators, whose POST the server
 *     refuses, and failed through alert().
 *   - The doctor never saw the drafted summary the patient received; the QR
 *     never showed (the client read a key the server did not send) and a
 *     withheld link gave no reason.
 *   - The lab card drew about twenty estimated values from the diagnosis when
 *     no lab was on file, and its modal assumed six-hourly results.
 *   - The Add Clinical Note modal had nothing that opened it.
 */
import { render, screen, act, fireEvent, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { openPatient } from '../../lib/orbBus'

vi.mock('../../api/sageAssessment', () => ({
  getLatestAssessmentForPatient: vi.fn(async () => ({ channelId: null, assessment: null, rows: [] })),
  listSageAssessments: vi.fn(async () => []),
  transitionAssessment: vi.fn(),
}))

const state: { detail: unknown } = { detail: null }

function detail(over: Record<string, unknown> = {}) {
  return {
    patient: {
      id: 'P021', name: 'Ravi Kumar', bed: '21', mrn: 'MRN-0021', ward: 'GM-2',
      diagnosis: 'Community acquired pneumonia', admission_date: '2026-09-28 06:30:00',
      age: 58, sex: 'M', comorbidities: ['Type 2 diabetes'], admission_id: 'ADM-0021',
      attending_doctor_name: 'Dr Meera Raman',
    },
    vitals: [{
      timestamp: '2026-10-02T09:00:00Z', heart_rate: 96, systolic_bp: 118,
      diastolic_bp: 70, respiratory_rate: 18, temperature: 37.4, spo2: 96,
    }],
    medications: [
      { id: 1, drug_name: 'Amoxicillin', dosage: '500 mg', route: 'Oral', frequency: 'TDS', status: 'active', unverified: false },
      { id: 2, drug_name: 'Ceftriaxone', dosage: '1 g', route: 'IV', frequency: 'OD', status: 'discontinued', unverified: false },
      { id: 3, drug_name: 'Warfarin', dosage: '5 mg', route: 'Oral', frequency: 'OD', status: 'active', unverified: true },
    ],
    notes: [
      { id: 7, doc_type: 'progress', title: 'Ward round', content: 'Chest clearer. Continue antibiotics.', created_at: '2026-10-02 08:15:00' },
    ],
    scribe_notes: [],
    lens_analyses: [],
    lab_observations: [],
    problems: [{ problem: 'Pneumonia', icd10_code: 'J18.9', onset_date: '2026-09-28' }],
    site_name: null,
    risk: { level: 'stable', score: 0.1, raw_news2: 1, drivers: [], breakdown: [], single_param_3: false },
    trend: { trend: 'stable', reason: null },
    ...over,
  }
}

type FetchOpts = { method?: string; body?: string; headers?: Record<string, string> }
/* Answers a test sets for one route prefix (an Error is thrown), checked
   before the defaults below. Cleared before every test. */
const routes: Record<string, unknown> = {}
// `_opts` is unused here: the review assertion reads it from mock.calls.
const apiFetch = vi.fn(async (path: string, _opts?: FetchOpts): Promise<unknown> => {
  for (const [prefix, answer] of Object.entries(routes)) {
    if (path.startsWith(prefix)) {
      if (answer instanceof Error) throw answer
      return answer
    }
  }
  if (path.includes('/allergies')) return { allergies: [{ substance: 'Penicillin', reaction: 'Rash', severity: 'moderate' }] }
  if (path.includes('/discharge-advisory')) {
    return {
      advisory_status: 'unknown', headline: 'Financial position: NOT KNOWN (this bill is incomplete)',
      reasons: ['2 clinical event(s) could not be priced and are missing from this bill'],
      message: 'Advisory only: this does not prevent the discharge.',
    }
  }
  if (path.includes('/discharge-summary/review')) {
    return { summary_id: 41, review_status: 'reviewed', reviewed_by_name: 'Dr Meera Raman', reviewed_at: '2026-10-03T01:00:00Z' }
  }
  return {}
})
const dischargePatient = vi.fn()
const addClinicalNote = vi.fn<(...a: unknown[]) => Promise<{ status: string; id: number }>>(async () => ({ status: 'success', id: 99 }))

vi.mock('../../api/client', () => ({
  apiFetch: (path: string, opts?: FetchOpts) => apiFetch(path, opts),
  errorDetailMessage: (detail: unknown, status: number) => {
    if (typeof detail === 'string') return detail
    const message = (detail as { message?: unknown } | null)?.message
    return typeof message === 'string' ? message : `The server answered ${status}.`
  },
  getOpenEscalations: vi.fn(async () => []),
  resolveEscalation: vi.fn(),
  getPatientDetail: vi.fn(async () => state.detail),
  getAdministrations: vi.fn(async () => []),
  givenAcrossAllergy: () => false,
  dischargePatient: (...a: unknown[]) => dischargePatient(...a),
  addClinicalNote: (...a: unknown[]) => addClinicalNote(...a),
  addMedication: vi.fn(),
  isSafetyAckRequired: () => false,
  getPatientForecast: vi.fn(() => new Promise(() => {})),
  getTimelineProjection: vi.fn(() => new Promise(() => {})),
  getDischargeQrObjectUrl: vi.fn(async () => 'blob:qr'),
  prefetchSageContext: vi.fn(async () => {}),
  getUnseenCounts: vi.fn(async () => ({})),
  administerMedication: vi.fn(),
  transitionSageAction: vi.fn(),
  getChannels: vi.fn(async () => []),
  getUser: () => ({ id: 'DOC1', name: 'Dr Meera Raman', role: 'doctor' }),
  buildHeaders: () => ({}),
  socket: { on: vi.fn(), off: vi.fn(), emit: vi.fn() },
}))

vi.mock('../../components/sage/SageInsightsFeed', () => ({ default: () => null }))
vi.mock('../../components/sage/SageAlertAssessment', () => ({ default: () => null }))
vi.mock('../../components/sage/WardBrief', () => ({ default: () => null }))
vi.mock('../../components/vigil/CareBundleTimer', () => ({ default: () => null }))
vi.mock('../../components/vigil/PatientStory', () => ({ default: () => null }))
vi.mock('../../components/ClinicalCourseTimeline', () => ({ ClinicalCourseTimeline: () => null }))

import VigilPanel from './VigilPanel'
import { resetModuleFlags, setModuleFlags } from '../../shellConfig'
import { buildLabTrends, labGraphData } from '../../lib/labTrends'
import { ActivePatientProvider } from '../../contexts/ActivePatientProvider'
import { ActiveViewProvider } from '../../contexts/ActiveViewProvider'

beforeEach(() => {
  vi.clearAllMocks()
  for (const key of Object.keys(routes)) delete routes[key]
  resetModuleFlags()
  state.detail = detail()
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => [], headers: { get: () => '0' },
  })))
  window.print = vi.fn()
})

async function openChart(role: string) {
  render(
    <ActivePatientProvider>
      <ActiveViewProvider>
        <VigilPanel role={role} />
      </ActiveViewProvider>
    </ActivePatientProvider>,
  )
  await act(async () => { openPatient('P021', 'Ravi Kumar') })
  await screen.findByText('Ravi Kumar')
}

/* ══════════════════════════════════════════════════════════════════════════ */

describe('Print record', () => {
  it('prints the inpatient record, never a discharge summary', async () => {
    await openChart('doctor')
    fireEvent.click(screen.getByRole('button', { name: /Print record/ }))
    const record = document.body.querySelector('[data-testid="inpatient-record-print"]') as HTMLElement
    expect(record).not.toBeNull()
    expect(record).toHaveTextContent('INPATIENT RECORD')
    expect(record).not.toHaveTextContent('DISCHARGE SUMMARY')
    expect(record).not.toHaveTextContent(/Discharged/i)
    // What the chart holds, and nothing it does not.
    expect(record).toHaveTextContent('MRN-0021')
    expect(record).toHaveTextContent('ADM-0021')
    expect(record).toHaveTextContent('Penicillin (Rash, moderate)')
    expect(record).toHaveTextContent('Pneumonia (J18.9)')
    expect(record).toHaveTextContent('Amoxicillin')
    expect(record).not.toHaveTextContent('Ceftriaxone')
    expect(record).toHaveTextContent('Chest clearer. Continue antibiotics.')
    expect(record).not.toHaveTextContent(/FOR 5 DAYS|Conservatively|No H\/o|hemodynamically/i)
  })
})

describe('Discharge', () => {
  it('is not offered to a nurse, whose POST the server refuses', async () => {
    await openChart('nurse')
    expect(screen.queryByRole('button', { name: /^Discharge$/ })).toBeNull()
  })

  it('shows the doctor the unreviewed draft, the billing advisory, and why no link was issued', async () => {
    dischargePatient.mockResolvedValue({
      status: 'success', summary_id: 41, review_status: 'draft', drafted_by: 'assistant',
      summary: 'DISCHARGE SUMMARY\nAdmitted with pneumonia. Treated with amoxicillin.',
      discharged_at: '2026-10-03 01:00:00', timeline: [],
      summary_url: null, qr_code_url: null, secure_link_issued: false,
      secure_link_reason: 'No date of birth on file, so no secure link can be opened.',
    })
    await openChart('doctor')
    fireEvent.click(screen.getByRole('button', { name: /^Discharge$/ }))

    const advisory = await screen.findByTestId('discharge-billing-advisory')
    expect(advisory).toHaveTextContent('NOT KNOWN')
    expect(advisory).toHaveTextContent('Advisory only')

    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Confirm discharge/ })) })

    const draft = await screen.findByTestId('discharge-draft')
    expect(draft).toHaveTextContent('AI-drafted, not reviewed')
    expect(draft).toHaveTextContent('Admitted with pneumonia. Treated with amoxicillin.')
    expect(screen.getByTestId('discharge-no-link')).toHaveTextContent('No date of birth on file')

    fireEvent.click(screen.getByRole('button', { name: /release it to the patient/ }))
    await waitFor(() => expect(within(screen.getByTestId('discharge-draft'))
      .getByText(/Reviewed by Dr Meera Raman, released to the patient/)).toBeInTheDocument())
    const reviewCall = apiFetch.mock.calls.find(([p]) => String(p).includes('/discharge-summary/review'))
    expect(reviewCall).toBeTruthy()
    expect(JSON.parse(reviewCall![1]?.body ?? '')).toEqual({ summary_id: 41 })
  })

  it('tells the desk the QR is an in-hospital handover, never a link for home', async () => {
    // jsdom has no object URLs; the QR image loader uses them.
    URL.createObjectURL = vi.fn(() => 'blob:qr')
    URL.revokeObjectURL = vi.fn()
    dischargePatient.mockResolvedValue({
      status: 'success', summary_id: 42, review_status: 'draft', drafted_by: 'assistant',
      summary: 'DISCHARGE SUMMARY\nAdmitted with pneumonia.', discharged_at: '2026-10-03 01:00:00', timeline: [],
      summary_url: 'https://orb.hospital.local:8000/discharge/tok', qr_code_url: '/api/vigil/patients/P021/discharge-qr/q.png',
      secure_link_issued: true, secure_link_reason: null,
    })
    await openChart('doctor')
    fireEvent.click(screen.getByRole('button', { name: /^Discharge$/ }))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Confirm discharge/ })) })
    const hint = await screen.findByTestId('discharge-qr-hint')
    expect(hint).toHaveTextContent('Before the patient leaves: release the summary to them')
    expect(hint).toHaveTextContent("patient Wi-Fi")
    expect(hint).toHaveTextContent('save a copy')
    expect(hint).toHaveTextContent('It does not open outside the hospital.')
  })

  it('reports a failed discharge inside the modal, not through alert()', async () => {
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {})
    dischargePatient.mockRejectedValue(new Error('Patient is not currently admitted'))
    await openChart('doctor')
    fireEvent.click(screen.getByRole('button', { name: /^Discharge$/ }))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /Confirm discharge/ })) })
    expect(await screen.findByRole('alert')).toHaveTextContent('Patient is not currently admitted')
    expect(alertSpy).not.toHaveBeenCalled()
  })
})

/* Beds and admissions (ORB_ADT, opt-in, off by default; adt-9): how the stay
   ended. Off, the discharge screen is exactly what it was and posts no body.
   On, it asks, routes LAMA, absconded and death to End of stay, and a DAMA
   needs the MRN typed back and a reason. */
describe('How the stay ended (Beds and admissions)', () => {
  const DRAFT = {
    status: 'success', summary_id: 43, review_status: 'draft', drafted_by: 'assistant',
    summary: 'DISCHARGE SUMMARY\nAdmitted with pneumonia.', discharged_at: '2026-10-06 01:00:00', timeline: [],
    summary_url: null, qr_code_url: null, secure_link_issued: false, secure_link_reason: 'No date of birth on file.',
  }

  function dischargePosts() {
    return apiFetch.mock.calls.filter(([p, o]) => p === '/vigil/patients/P021/discharge' && o?.method === 'POST')
  }

  /** Opens the discharge screen and lets its reads (the billing advisory,
   *  the open chains) answer before the test goes on. */
  async function openDischarge() {
    await openChart('doctor')
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: /^Discharge$/ })) })
    return screen.getByRole('dialog', { name: /Discharge Ravi Kumar/ })
  }

  it('with the module off there is no select, and the discharge posts no body', async () => {
    dischargePatient.mockResolvedValue(DRAFT)
    const dialog = await openDischarge()
    expect(within(dialog).queryByLabelText('How the stay ended')).toBeNull()
    expect(screen.queryByTestId('discharge-end-type')).toBeNull()
    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: /Confirm discharge/ })) })
    expect(dischargePatient).toHaveBeenCalledTimes(1)
    expect(dischargePatient).toHaveBeenCalledWith('P021')
    expect(dischargePosts()).toHaveLength(0)
    expect(await screen.findByTestId('discharge-draft')).toBeInTheDocument()
  })

  it('with the module off an ended stay offers no reopen', async () => {
    state.detail = detail({ patient: { ...(detail().patient as Record<string, unknown>), status: 'discharged' } })
    await openChart('doctor')
    expect(screen.queryByRole('button', { name: 'Recorded in error' })).toBeNull()
  })

  it('with the module on it asks how the stay ended, and a routine discharge posts its type', async () => {
    setModuleFlags({ adt: true })
    routes['/vigil/patients/P021/discharge'] = DRAFT
    const dialog = await openDischarge()
    const select = within(dialog).getByLabelText('How the stay ended') as HTMLSelectElement
    expect(select.value).toBe('routine')
    expect(within(select).getAllByRole('option').map((o) => (o as HTMLOptionElement).value)).toEqual(
      ['routine', 'referred_out', 'transferred_out', 'dama', 'lama', 'absconded', 'death'])
    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: /Confirm discharge/ })) })
    expect(dischargePatient).not.toHaveBeenCalled()
    const [call] = dischargePosts()
    expect(JSON.parse(call[1]?.body ?? '{}')).toEqual({ discharge_type: 'routine' })
    expect(await screen.findByTestId('discharge-draft')).toHaveTextContent('Admitted with pneumonia.')
  })

  it.each(['referred_out', 'transferred_out'])('a %s discharge posts its type and no read-back', async (endType) => {
    setModuleFlags({ adt: true })
    routes['/vigil/patients/P021/discharge'] = DRAFT
    const dialog = await openDischarge()
    fireEvent.change(within(dialog).getByLabelText('How the stay ended'), { target: { value: endType } })
    expect(within(dialog).queryByLabelText('Type the MRN from the wristband to confirm')).toBeNull()
    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: /Confirm discharge/ })) })
    const [call] = dischargePosts()
    expect(JSON.parse(call[1]?.body ?? '{}')).toEqual({ discharge_type: endType })
  })

  it("a DAMA needs a reason and the typed MRN, and the server's read_back_mismatch is shown in words", async () => {
    setModuleFlags({ adt: true })
    routes['/vigil/patients/P021/discharge'] = Object.assign(new Error('refused'), {
      status: 409,
      detail: { error: 'read_back_mismatch', message: "The MRN typed does not match this patient's record. Check the wristband and type it again. Nothing was recorded." },
    })
    const dialog = await openDischarge()
    fireEvent.change(within(dialog).getByLabelText('How the stay ended'), { target: { value: 'dama' } })
    const confirm = within(dialog).getByRole('button', { name: /Confirm discharge/ })
    expect(confirm).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText('Reason the patient is leaving against advice'), { target: { value: 'Family wants a second opinion' } })
    expect(confirm).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText('Type the MRN from the wristband to confirm'), { target: { value: 'MRN-0012' } })
    expect(within(dialog).getByText('This does not match the MRN on file.')).toBeInTheDocument()
    expect(confirm).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText('Type the MRN from the wristband to confirm'), { target: { value: 'mrn-0021' } })
    expect(confirm).toBeEnabled()
    await act(async () => { fireEvent.click(confirm) })
    const [call] = dischargePosts()
    expect(JSON.parse(call[1]?.body ?? '{}')).toEqual({
      discharge_type: 'dama', confirm_mrn: 'mrn-0021', reason: 'Family wants a second opinion',
    })
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      "The discharge did not complete. The MRN typed does not match this patient's record. Check the wristband and type it again. Nothing was recorded.")
    expect(screen.queryByTestId('discharge-draft')).toBeNull()
  })

  it('a DAMA for a patient with no MRN on file cannot be confirmed, and says so', async () => {
    setModuleFlags({ adt: true })
    state.detail = detail({ patient: { ...(detail().patient as Record<string, unknown>), mrn: '' } })
    const dialog = await openDischarge()
    fireEvent.change(within(dialog).getByLabelText('How the stay ended'), { target: { value: 'dama' } })
    fireEvent.change(within(dialog).getByLabelText('Reason the patient is leaving against advice'), { target: { value: 'Going home' } })
    expect(within(dialog).getByLabelText('Type the MRN from the wristband to confirm')).toBeDisabled()
    expect(dialog).toHaveTextContent('No MRN is on file for this patient, so a DAMA cannot be confirmed here.')
    expect(within(dialog).getByRole('button', { name: /Confirm discharge/ })).toBeDisabled()
    expect(dischargePosts()).toHaveLength(0)
  })

  it('a lost reply is not called a failure: the doctor is told to reload before trying again', async () => {
    setModuleFlags({ adt: true })
    routes['/vigil/patients/P021/discharge'] = new TypeError('Failed to fetch')
    const dialog = await openDischarge()
    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: /Confirm discharge/ })) })
    const alert = await within(dialog).findByRole('alert')
    expect(alert).toHaveTextContent(
      'Could not reach Orb, so it is not known whether the discharge was recorded. Reload the chart to see before you try again.')
    expect(alert).not.toHaveTextContent('did not complete')
    expect(alert).not.toHaveTextContent('never recorded twice')
  })

  it.each([
    ['death', 'Death'],
    ['lama', 'Left against medical advice (LAMA)'],
    ['absconded', 'Absconded'],
  ])('%s opens End of stay, which posts to the end route only once the MRN matches', async (endType, label) => {
    setModuleFlags({ adt: true })
    routes['/adt/admissions/ADM-0021/end'] = {
      status: 'ended', replayed: false, admission_id: 'ADM-0021', patient_id: 'P021', end_type: endType,
      discharged_at: '2026-10-06T04:30:00Z', movement_id: 'MOV-9', discharge_link_issued: false,
      open_escalations: [], bundles_cancelled: [],
    }
    const dialog = await openDischarge()
    fireEvent.change(within(dialog).getByLabelText('How the stay ended'), { target: { value: endType } })
    expect(dialog).toHaveTextContent('is recorded through End of stay')
    expect(within(dialog).queryByRole('button', { name: /Confirm discharge/ })).toBeNull()
    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: 'Continue to end of stay' })) })

    // The discharge screen closed; End of stay is open with the type chosen.
    expect(screen.queryByRole('dialog', { name: /Discharge Ravi Kumar/ })).toBeNull()
    const endStay = screen.getByRole('dialog', { name: 'End of stay' })
    expect(within(endStay).getByRole('radio', { name: label })).toBeChecked()
    const readback = within(endStay).getByText('Ward and bed').closest('dl') as HTMLElement
    expect(readback).toHaveTextContent('MRN-0021')
    const record = within(endStay).getByRole('button', { name: endType === 'death' ? 'Record death' : 'Record end of stay' })
    if (endType !== 'death') {
      fireEvent.change(within(endStay).getByLabelText('Reason'), { target: { value: 'Left the ward at night' } })
    }
    expect(record).toBeDisabled()
    fireEvent.change(within(endStay).getByLabelText('Type the MRN from the wristband to confirm'), { target: { value: 'MRN-0021' } })
    expect(record).toBeEnabled()
    await act(async () => { fireEvent.click(record) })

    expect(dischargePatient).not.toHaveBeenCalled()
    expect(dischargePosts()).toHaveLength(0)
    const ends = apiFetch.mock.calls.filter(([p, o]) => p === '/adt/admissions/ADM-0021/end' && o?.method === 'POST')
    expect(ends).toHaveLength(1)
    const body = JSON.parse(ends[0][1]?.body ?? '{}')
    expect(body.end_type).toBe(endType)
    expect(body.confirm_mrn).toBe('MRN-0021')
    expect(ends[0][1]?.headers?.['Idempotency-Key']).toBeTruthy()
    expect(await screen.findByTestId('end-stay-result')).toBeInTheDocument()
  })

  it("an ended stay offers 'Recorded in error', which posts the reopen", async () => {
    setModuleFlags({ adt: true })
    state.detail = detail({ patient: { ...(detail().patient as Record<string, unknown>), status: 'discharged' } })
    routes['/adt/admissions/ADM-0021/reopen'] = {
      status: 'reopened', replayed: false, admission_id: 'ADM-0021', patient_id: 'P021', movement_id: 'MOV-10',
      corrects_movement_id: 'MOV-9', corrected_end_type: 'death', bed_retaken: true, awaiting_bed: false,
      ward: 'GM-2', bed: '21', portal_login_restored: false, bundles_not_restored: [],
    }
    await openChart('doctor')
    fireEvent.click(screen.getByRole('button', { name: 'Recorded in error' }))
    const dialog = screen.getByRole('dialog', { name: 'Reopen a stay recorded in error' })
    fireEvent.change(within(dialog).getByLabelText('Why the end was recorded in error'), { target: { value: 'Wrong patient' } })
    fireEvent.change(within(dialog).getByLabelText('Type the MRN from the wristband to confirm'), { target: { value: 'MRN-0021' } })
    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: 'Reopen stay' })) })
    const reopens = apiFetch.mock.calls.filter(([p, o]) => p === '/adt/admissions/ADM-0021/reopen' && o?.method === 'POST')
    expect(reopens).toHaveLength(1)
    expect(JSON.parse(reopens[0][1]?.body ?? '{}')).toEqual({ reason: 'Wrong patient', confirm_mrn: 'MRN-0021' })
    expect(await screen.findByTestId('reopen-stay-result')).toHaveTextContent('Back in GM-2, bed 21.')
  })

  it('a nurse is offered neither the discharge nor the reopen', async () => {
    setModuleFlags({ adt: true })
    state.detail = detail({ patient: { ...(detail().patient as Record<string, unknown>), status: 'discharged' } })
    await openChart('nurse')
    expect(screen.queryByRole('button', { name: /^Discharge$/ })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Recorded in error' })).toBeNull()
  })

  it('no rendered sentence on the discharge screen carries an em dash', async () => {
    setModuleFlags({ adt: true })
    const dialog = await openDischarge()
    for (const value of ['dama', 'death']) {
      fireEvent.change(within(dialog).getByLabelText('How the stay ended'), { target: { value } })
      expect(dialog.textContent).not.toMatch(/\u2014/)
    }
  })
})

describe('the lab card', () => {
  it('shows no values when no lab result is recorded', async () => {
    await openChart('doctor')
    expect(await screen.findByTestId('labs-empty')).toHaveTextContent('No lab results recorded')
    expect(screen.queryByRole('button', { name: /Open trend chart for/ })).toBeNull()
    expect(screen.queryByText('Estimated')).toBeNull()
  })

  it('plots recorded results at the times they were observed', async () => {
    state.detail = detail({
      lab_observations: [
        { id: 1, observation_name: 'Creatinine', value_numeric: 1.6, unit: 'mg/dL', reference_range: '0.7-1.3', abnormal_flag: 'H', status: 'final', observed_at: '2026-10-01 06:00:00' },
        { id: 2, observation_name: 'Creatinine', value_numeric: 2.1, unit: 'mg/dL', reference_range: '0.7-1.3', abnormal_flag: 'H', status: 'final', observed_at: '2026-10-02 18:30:00' },
      ],
    })
    await openChart('doctor')
    const tile = await screen.findByRole('button', { name: /Open trend chart for Creatinine, currently 2.1mg\/dL/ })
    expect(tile).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /Open trend chart for/ })).toHaveLength(1)
  })
})

describe('buildLabTrends / labGraphData', () => {
  it('builds nothing from nothing: there is no estimator', () => {
    expect(buildLabTrends([], [])).toEqual([])
    expect(buildLabTrends(undefined, undefined)).toEqual([])
  })

  it('keeps each result at its own observed time, 36.5 hours apart, not six', () => {
    const [lab] = buildLabTrends([], [
      { observation_name: 'Potassium', value_numeric: 5.9, unit: 'mmol/L', reference_range: '3.5-5.0', observed_at: '2026-10-02 18:30:00' },
      { observation_name: 'Potassium', value_numeric: 5.2, unit: 'mmol/L', reference_range: '3.5-5.0', observed_at: '2026-10-01 06:00:00' },
    ])
    expect(lab.points).toEqual([5.2, 5.9])
    const pts = labGraphData(lab)
    expect(pts[1].t - pts[0].t).toBe(36.5 * 3600 * 1000)
    expect(pts[0].t).toBe(Date.UTC(2026, 9, 1, 6, 0, 0))
    expect(lab.status).toBe('high')
  })

  it('takes the range from the newest result, whatever order the rows arrive in', () => {
    const older = { observation_name: 'Sodium', value_numeric: 131, unit: 'mmol/L', reference_range: '130-145', observed_at: '2026-09-01 06:00:00' }
    const newer = { observation_name: 'Sodium', value_numeric: 133, unit: 'mmol/L', reference_range: '135-145', observed_at: '2026-10-02 06:00:00' }
    for (const rows of [[older, newer], [newer, older]]) {
      const [lab] = buildLabTrends([], rows)
      expect(lab.current).toBe(133)
      expect([lab.refLow, lab.refHigh, lab.refText]).toEqual([135, 145, '135-145'])
      expect(lab.status).toBe('low')
    }
  })

  it('does not judge a result with no range and no flag', () => {
    const [lab] = buildLabTrends([], [
      { observation_name: 'Troponin I', value_numeric: 0.9, unit: 'ng/mL', reference_range: null, observed_at: '2026-10-02 06:00:00' },
    ])
    expect(lab.status).toBe('unranged')
    expect(lab.refLow).toBeNull()
  })
})

describe('Add note', () => {
  it('opens the note modal from the chart and saves the note', async () => {
    await openChart('doctor')
    fireEvent.click(screen.getByRole('button', { name: 'Add note' }))
    const box = await screen.findByRole('textbox', { name: 'Clinical note' })
    fireEvent.change(box, { target: { value: 'Reviewed. Plan discharge tomorrow.' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save Note' })) })
    expect(addClinicalNote).toHaveBeenCalledWith('P021', 'Reviewed. Plan discharge tomorrow.')
  })

  it('is reachable from the Notes modal header too', async () => {
    await openChart('doctor')
    fireEvent.click(document.getElementById('btn-notes') as HTMLElement)
    const dialog = await screen.findByRole('dialog', { name: /Clinical Notes History/ })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add note' }))
    expect(await screen.findByRole('textbox', { name: 'Clinical note' })).toBeInTheDocument()
  })

  it('is not offered to a nurse, who cannot write the chart note', async () => {
    await openChart('nurse')
    expect(screen.queryByRole('button', { name: 'Add note' })).toBeNull()
  })
})
