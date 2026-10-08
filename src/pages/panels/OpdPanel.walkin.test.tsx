/**
 * The Outpatients tab: the walk-in (opd-8, reusing the Register view) and the
 * allergy review (opd-10a, ruling OPD-S7).
 *
 *  1. ADD WALK-IN opens the front desk's Register view on this tab (nothing
 *     is fetched for it until it is opened), and a visit registered there
 *     refetches the doctor's queue. The patient confirms who they are first.
 *  2. THE RECORD SAYS WHAT WAS ASKED: "Allergies not yet asked" with no
 *     review; "No known allergies, asked by X on date"; "could not be
 *     obtained" with the reason; a "no known allergies" the chart now
 *     contradicts asks for a new review.
 *  3. THE REVIEW: offered on the doctor's own visit; "No known allergies" is
 *     not offered while an allergy is charted; the review is posted to the
 *     visit and the record is read again.
 *  4. Neither new screen publishes a patient into the active view contexts.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { formatClinicalDateTime } from '../../lib/datetime'
import OpdPanel from './OpdPanel'

const h = vi.hoisted(() => ({ apiFetch: vi.fn() }))

vi.mock('../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => h.apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
  getUser: () => ({ role: 'doctor' }),
  socket: { connected: true, on: () => {}, off: () => {}, emit: vi.fn() },
}))

const DEPT = 'Cardiology'
const ASKED_AT = '2026-10-05T04:30:00+00:00'

function visit(id: string, token: number, extra: Record<string, unknown> = {}) {
  return {
    id, patient_id: `P-${id}`, patient_name: `Patient ${id}`, patient_mrn: `UHID-${id}`,
    patient_age: 50, patient_sex: 'F', department: DEPT, doctor_id: 'DOC-1',
    doctor_name: 'Dr Iyer', visit_date: '2026-10-05', token_number: token, visit_type: 'new',
    chief_complaint: 'cough', status: 'waiting', registered_at: '2026-10-05T03:00:00+00:00',
    called_at: null, completed_at: null, cancelled_at: null, cancel_reason: null,
    consultation_charge_id: null, notes: null, waiting_minutes: 12, consultation_minutes: null,
    priority: 'normal', priority_label: 'Normal', priority_reason: null, room: null,
    call_count: 0, skipped: false, skipped_at: null, cancel_reason_code: null,
    cancel_reason_label: null, disposition: null, disposition_at: null,
    consultation_status: null,
    ...extra,
  }
}

const QUEUE = {
  visit_date: '2026-10-05', department: DEPT, doctor_id: 'DOC-1',
  current: [], waiting: [visit('V-TWO', 2, { patient_name: 'Chitra Second' })],
  skipped: [], unassigned: [], completed_count: 0, total: 1,
  priorities: [{ code: 'normal', label: 'Normal' }], poll_seconds: 30,
}

function record(extra: Record<string, unknown> = {}) {
  return {
    patient: { id: 'P-V-TWO', name: 'Chitra Second', uhid: 'UHID-V-TWO', age: 50,
      age_recorded_on: null, sex: 'F', preferred_language: null, record_status: 'registered',
      inpatient_now: false, date_of_birth_on_file: true },
    access: { arm: 'opd_visit', visit_id: 'V-TWO', visit_date: '2026-10-05', scope_until: '2026-10-12' },
    allergies: [], allergy_status: 'none_recorded', problems: [], opd_visits: [], results: [],
    consultations: [], consultations_available: true, allergy_assessment: null,
    ...extra,
  }
}

function assessment(status: string, extra: Record<string, unknown> = {}) {
  return { id: 7, status, status_label: 'x', substances: [], reason: null, note: null,
    opd_visit_id: 'V-OLD', department: DEPT, assessed_by: 'DOC-1', assessed_by_name: 'Dr Iyer',
    assessed_at: ASKED_AT, ...extra }
}

/** A review as the visit-addressed answers carry it: what it said and when,
 *  nothing it names (the full review is on the record view only). */
function summary(status: string, label = 'x') {
  return { status, status_label: label, assessed_at: ASKED_AT }
}

const NOTE_VIEW = {
  visit_id: 'V-TWO', patient_id: 'P-V-TWO', visit_status: 'waiting', consultation: null,
  allergy_review: { status: 'not_reviewed', can_sign: false, charted_count: 0,
    reviewed_this_visit: false, basis: null, assessment: null },
  patient_admitted: false,
}

type Handler = (options?: RequestInit) => unknown
function routeApi(overrides: Record<string, Handler> = {}) {
  h.apiFetch.mockImplementation(async (path: string, options?: RequestInit) => {
    for (const [prefix, fn] of Object.entries(overrides)) {
      if (path.startsWith(prefix)) return fn(options)
    }
    if (path === '/opd/my-queue') return QUEUE
    if (path === '/opd/meta') return { feature: 'opd', enabled: true,
      settings: { scope_days: 7, charge_at: 'completion', queue_poll_seconds: 30 }, dispositions: [] }
    if (path.startsWith('/opd/patients/')) return record()
    if (path.endsWith('/consultation')) return NOTE_VIEW
    throw new Error(`unrouted ${path}`)
  })
}

function calls(prefix: string) {
  return h.apiFetch.mock.calls.filter(([p]) => String(p).startsWith(prefix))
}

async function openChitra() {
  render(<OpdPanel />)
  fireEvent.click(await screen.findByRole('button', { name: /Open the record of Chitra Second/ }))
  return screen.findByRole('region', { name: 'Allergies' })
}

beforeEach(() => { h.apiFetch.mockReset() })
afterEach(() => cleanup())

describe('add walk-in', () => {
  it('opens the Register view only when asked, and a registered visit refetches the queue', async () => {
    routeApi({
      '/registration/config': () => ({ uhid_source: 'hospital', uhid_ready: true,
        duplicate_mode: 'warn', abdm_enabled: false, abha_notice: '', search_min_chars: 3,
        search_max_results: 20, reason_min_chars: 8 }),
      '/frontdesk/opd/doctors': () => ({ doctors: [{ id: 'DOC-1', name: 'Dr Iyer', department: DEPT }] }),
      '/registration/search': () => ({ results: [{ patient_id: 'P-NEW', uhid: 'UH-9', name: 'Walkin Wanda',
        sex: 'F', age: 30, age_stated: true, phone_last4: '4321', last_visit_date: null,
        inpatient_now: false }], truncated: false, kinds: ['name'], min_chars: 3, max_results: 20 }),
      '/registration/identity-check': () => ({ result: 'match', checked: ['name', 'age_years', 'phone_last4'],
        checked_at: '2026-10-05T05:00:00Z' }),
      '/frontdesk/opd/visits': () => visit('V-NEW', 3, { patient_name: 'Walkin Wanda' }),
    })
    render(<OpdPanel />)
    await screen.findByText('Chitra Second')
    expect(calls('/registration/')).toHaveLength(0)
    fireEvent.click(screen.getByRole('button', { name: 'Add walk-in' }))
    const panel = screen.getByRole('region', { name: 'Add a walk-in' })
    expect(within(panel).getByText('Add a walk-in')).toBeInTheDocument()
    fireEvent.change(within(panel).getByLabelText('UHID, full phone number or name'), { target: { value: 'Wanda' } })
    fireEvent.click(within(panel).getByRole('button', { name: 'Search' }))
    fireEvent.click(await within(panel).findByRole('button', { name: /Choose Walkin Wanda/ }))
    const dialog = screen.getByRole('dialog', { name: 'Confirm who the patient is' })
    fireEvent.change(within(dialog).getByLabelText('Their name, as they say it'), { target: { value: 'Walkin Wanda' } })
    fireEvent.click(within(dialog).getByLabelText('Age in years'))
    fireEvent.change(within(dialog).getByLabelText('Age they state, in years'), { target: { value: '30' } })
    fireEvent.change(within(dialog).getByLabelText('Last 4 digits of their phone'), { target: { value: '4321' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Check' }))
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Continue' }))
    const before = calls('/opd/my-queue').length
    fireEvent.click(await within(panel).findByRole('button', { name: 'Register visit and issue token' }))
    expect(await screen.findByText('Token 3 registered for Walkin Wanda.')).toBeInTheDocument()
    await waitFor(() => expect(calls('/opd/my-queue').length).toBeGreaterThan(before))
    const sent = JSON.parse(String((calls('/frontdesk/opd/visits')[0][1] as RequestInit).body))
    expect(sent.doctor_id).toBe('DOC-1')
    expect(sent.department).toBeUndefined()
    expect(sent.identity_check).toEqual({ name: 'Walkin Wanda', age_years: 30, phone_last4: '4321' })
  })
})

describe('the allergy status on the record', () => {
  it('says not yet asked when no review is on record', async () => {
    routeApi()
    const allergies = await openChitra()
    expect(within(allergies).getByText('Allergies not yet asked.')).toBeInTheDocument()
    expect(within(allergies).getByText(/This is not the same as no known allergies/)).toBeInTheDocument()
  })

  it('does not say not yet asked over allergies charted without a review', async () => {
    routeApi({ '/opd/patients/': () => record({
      allergies: [{ substance: 'penicillin', reaction: 'rash', severity: 'severe' }],
      allergy_status: 'recorded' }) })
    const allergies = await openChitra()
    expect(within(allergies).getByText('No allergy review on record.')).toBeInTheDocument()
    expect(within(allergies).queryByText('Allergies not yet asked.')).toBeNull()
    expect(within(allergies).getByText(/penicillin/)).toBeInTheDocument()
  })

  it('names who asked and when for no known allergies', async () => {
    routeApi({ '/opd/patients/': () => record({ allergy_assessment: assessment('none_known') }) })
    const allergies = await openChitra()
    expect(within(allergies).getByText(
      `No known allergies, asked by Dr Iyer on ${formatClinicalDateTime(ASKED_AT)}.`)).toBeInTheDocument()
    expect(within(allergies).queryByText(/nobody may have asked/)).toBeNull()
  })

  it('gives the reason when allergies could not be obtained', async () => {
    routeApi({ '/opd/patients/': () => record({
      allergy_assessment: assessment('unable_to_obtain', { reason: 'Patient aphasic' }) }) })
    const allergies = await openChitra()
    expect(within(allergies).getByText(/Allergies could not be obtained \(Patient aphasic\)/))
      .toBeInTheDocument()
  })

  it('asks for a new review when the chart now contradicts no known allergies', async () => {
    routeApi({ '/opd/patients/': () => record({
      allergies: [{ substance: 'latex', reaction: null, severity: 'unknown' }],
      allergy_status: 'recorded', allergy_assessment: assessment('none_known') }) })
    const allergies = await openChitra()
    expect(within(allergies).getByText(/allergies have been charted since\. Review again\./))
      .toBeInTheDocument()
  })
})

describe('recording the review', () => {
  it('posts the review to the visit and reads the record again', async () => {
    let posted: Record<string, unknown> | null = null
    routeApi({ '/opd/visits/V-TWO/allergy-review': options => {
      posted = JSON.parse(String(options?.body))
      return { visit_id: 'V-TWO', patient_id: 'P-V-TWO', charted: ['penicillin'],
        reactivated: [], assessment: summary('allergies_recorded', 'Allergies recorded'),
        allergy_review: { status: 'allergies_recorded', can_sign: true, charted_count: 1 } }
    } })
    const allergies = await openChitra()
    const reads = calls('/opd/patients/').length
    fireEvent.click(within(allergies).getByRole('button', { name: 'Record allergy review' }))
    const dialog = screen.getByRole('dialog', { name: 'Allergy review for Chitra Second' })
    const save = within(dialog).getByRole('button', { name: 'Save allergy review' })
    expect(save).toBeDisabled()
    fireEvent.click(within(dialog).getByLabelText('They have allergies'))
    expect(save).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText('Allergies, one per line'),
      { target: { value: 'penicillin\n\n sulfa ' } })
    fireEvent.click(save)
    expect(await screen.findByText('Allergy review saved for Chitra Second: allergies recorded.'))
      .toBeInTheDocument()
    expect(posted).toEqual({ status: 'allergies_recorded', substances: ['penicillin', 'sulfa'] })
    await waitFor(() => expect(calls('/opd/patients/').length).toBeGreaterThan(reads))
  })

  it('does not offer no known allergies while an allergy is charted', async () => {
    routeApi({ '/opd/patients/': () => record({
      allergies: [{ substance: 'latex', reaction: null, severity: 'unknown' }],
      allergy_status: 'recorded' }) })
    const allergies = await openChitra()
    fireEvent.click(within(allergies).getByRole('button', { name: 'Record allergy review' }))
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByLabelText(/No known allergies/)).toBeDisabled()
    expect(within(dialog).getByText('Not available while an allergy is on the chart.')).toBeInTheDocument()
    fireEvent.click(within(dialog).getByLabelText('Could not be obtained'))
    expect(within(dialog).getByRole('button', { name: 'Save allergy review' })).toBeDisabled()
    fireEvent.change(within(dialog).getByLabelText('Why could they not be obtained?'),
      { target: { value: 'Unconscious' } })
    expect(within(dialog).getByRole('button', { name: 'Save allergy review' })).toBeEnabled()
  })

  it('says which review lets the note be signed', async () => {
    routeApi({ '/opd/visits/V-TWO/consultation': () => ({ ...NOTE_VIEW,
      allergy_review: { status: 'none_known', can_sign: true, charted_count: 0,
        reviewed_this_visit: false, basis: 'earlier_review', assessment: summary('none_known') } }) })
    await openChitra()
    expect(await screen.findByText(
      `Allergy review from an earlier visit, ${formatClinicalDateTime(ASKED_AT)}.`)).toBeInTheDocument()
    expect(screen.queryByText(/A note can be signed once/)).toBeNull()
  })
})

describe('what the new screens never touch', () => {
  it('neither the Register view nor the review dialog imports the active view contexts', () => {
    for (const file of ['src/pages/panels/frontdesk/RegisterPatientView.tsx',
      'src/pages/panels/frontdesk/PatientIndexSearch.tsx',
      'src/pages/panels/frontdesk/IdentityConfirmDialog.tsx',
      'src/pages/panels/opd/AllergyReviewDialog.tsx']) {
      const src = readFileSync(resolve(process.cwd(), file), 'utf8')
      for (const name of ['ActiveViewContext', 'ActivePatientContext', 'useActiveView',
        'useActivePatient', 'setViewSelection']) {
        expect(src, `${file} mentions ${name}`).not.toContain(name)
      }
    }
  })
})
