/**
 * The Register view (opd-6a): search first, confirm who the patient is, then
 * register the visit.
 *
 *  1. SEARCH FIRST: registering a new patient is offered only after a search,
 *     and a result card shows minimal fields only.
 *  2. WHO THE PATIENT IS (ruling OPD-S1): the dialog asks what the patient
 *     states, shows only match or no match, and never renders a stored date of
 *     birth or phone, even if one reached the client. The visit is sent with
 *     the stated identifiers.
 *  3. DUPLICATES (ruling OPD-S2): a possible duplicate needs a written reason
 *     before "Register as a new patient"; a probable one offers no override.
 *  4. THE NEW FORM: a stated age shows the "no date of birth, no discharge
 *     link" hint; the ABHA field exists only with ABDM on and says unverified;
 *     there is no allergy field (ruling OPD-S5). A new patient's visit carries
 *     what was typed and the UHID the registration returned.
 *  5. An account without registration.write is not offered a new
 *     registration; the feature going off is reported to the panel.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import RegisterPatientView from './RegisterPatientView'

const apiFetch = vi.fn()

vi.mock('../../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
}))

const DEPT = 'Cardiology'
const LEAKED_DOB = '1961-02-03'
const LEAKED_PHONE = '9876500000'

const CONFIG = {
  uhid_source: 'hospital', uhid_pattern_set: false, uhid_pattern_valid: true,
  uhid_series_configured: false, uhid_ready: true, duplicate_mode: 'warn',
  abdm_enabled: false, abha_notice: 'Recorded in Orb only: nothing was sent to ABDM.',
  search_min_chars: 3, search_max_results: 20, reason_min_chars: 8,
  thresholds: { probable: 70, possible: 50 }, id_document_types: [], allergies_at_desk: false,
}

const CARD = {
  patient_id: 'PAT-1', uhid: 'UH-1001', name: 'Anjali Rao', sex: 'F', age: 64,
  age_stated: false, phone_last4: '0000', last_visit_date: '2026-09-30', inpatient_now: false,
  // Never sent by the server. Here only to prove the screen never prints it.
  date_of_birth: LEAKED_DOB, phone: LEAKED_PHONE,
}

const ROSTER = { doctors: [
  { id: 'DOC-1', name: 'Dr Iyer', department: DEPT },
  { id: 'DOC-2', name: 'Dr Sen', department: 'Neurology' },
] }

function visitReply(overrides: Record<string, unknown> = {}) {
  return {
    id: 'OPD-9', patient_id: 'PAT-1', patient_name: 'Anjali Rao', patient_mrn: 'UH-1001',
    patient_age: 64, patient_sex: 'F', department: DEPT, doctor_id: 'DOC-1',
    doctor_name: 'Dr Iyer', visit_date: '2026-10-07', token_number: 12, visit_type: 'new',
    chief_complaint: null, status: 'waiting', registered_at: '2026-10-07T03:00:00+00:00',
    called_at: null, completed_at: null, cancelled_at: null, cancel_reason: null,
    consultation_charge_id: null, notes: null, waiting_minutes: 0, consultation_minutes: null,
    identity_check: { result: 'match', checked: ['name', 'date_of_birth', 'phone_last4'],
      checked_at: '2026-10-07T03:00:00Z' },
    ...overrides,
  }
}

function refusal(status: number, detail: Record<string, unknown>) {
  const err = new Error(String(detail.message ?? 'refused')) as Error & { status: number; detail: unknown }
  err.status = status
  err.detail = detail
  return err
}

type Handler = (options?: RequestInit) => unknown
function routeApi(extra: Record<string, Handler> = {}) {
  apiFetch.mockImplementation(async (path: string, options?: RequestInit) => {
    for (const [prefix, fn] of Object.entries(extra)) {
      if (path.startsWith(prefix)) return fn(options)
    }
    if (path === '/registration/config') return CONFIG
    if (path === '/frontdesk/opd/doctors') return ROSTER
    if (path.startsWith('/registration/search')) {
      return { results: [CARD], truncated: false, kinds: ['name'], min_chars: 3, max_results: 20 }
    }
    if (path === '/registration/identity-check') {
      return { result: 'match', checked: ['name', 'date_of_birth', 'phone_last4'],
        checked_at: '2026-10-07T03:00:00Z' }
    }
    if (path === '/frontdesk/opd/visits') return visitReply()
    throw new Error(`unrouted ${path}`)
  })
}

function bodyOf(path: string): Record<string, unknown> {
  const call = [...apiFetch.mock.calls].reverse().find(([p]) => p === path)
  if (!call) throw new Error(`no call to ${path}`)
  return JSON.parse(String((call[1] as RequestInit).body))
}

const deskHolds = (p: string) => ['registration.read', 'registration.write'].includes(p)

function renderView(props: Partial<React.ComponentProps<typeof RegisterPatientView>> = {}) {
  const onFeatureOff = vi.fn()
  const openView = vi.fn()
  render(<RegisterPatientView scopedDepartment={DEPT} openView={openView}
    onFeatureOff={onFeatureOff} holds={deskHolds} {...props} />)
  return { onFeatureOff, openView }
}

async function search(text = 'Anjali') {
  fireEvent.change(screen.getByLabelText('UHID, full phone number or name'), { target: { value: text } })
  fireEvent.click(screen.getByRole('button', { name: 'Search' }))
}

beforeEach(() => {
  apiFetch.mockReset()
  routeApi()
})
afterEach(() => cleanup())

describe('search first', () => {
  it('offers a new registration only after a search, and shows minimal fields', async () => {
    renderView()
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith('/registration/config', undefined))
    expect(screen.queryByRole('button', { name: 'Register a new patient' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Search' })).toBeDisabled()
    await search()
    const list = await screen.findByRole('list', { name: 'Matching patients' })
    expect(within(list).getByText('Anjali Rao')).toBeInTheDocument()
    expect(within(list).getByText(/UHID UH-1001 · Female · 64 y · phone ends 0000/)).toBeInTheDocument()
    expect(apiFetch.mock.calls.some(([p]) => p === '/registration/search?q=Anjali')).toBe(true)
    expect(screen.getByRole('button', { name: 'Register a new patient' })).toBeInTheDocument()
    expect(document.body.textContent).not.toContain(LEAKED_DOB)
    expect(document.body.textContent).not.toContain(LEAKED_PHONE)
  })

  it('does not offer a new registration to an account without registration.write', async () => {
    renderView({ holds: p => p === 'registration.read' })
    await search()
    await screen.findByText('Anjali Rao')
    expect(screen.queryByRole('button', { name: 'Register a new patient' })).toBeNull()
  })

  it('tells the panel when the registration feature has gone off', async () => {
    routeApi({ '/registration/config': () => { throw refusal(404, { error: 'feature_disabled' }) } })
    const { onFeatureOff } = renderView()
    await waitFor(() => expect(onFeatureOff).toHaveBeenCalled())
  })
})

describe('who the patient is', () => {
  async function openDialog() {
    renderView()
    await search()
    fireEvent.click(await screen.findByRole('button', { name: /Choose Anjali Rao/ }))
    return screen.getByRole('dialog', { name: 'Confirm who the patient is' })
  }

  function fillStated(dialog: HTMLElement, dob = '1961-02-03') {
    fireEvent.change(within(dialog).getByLabelText('Their name, as they say it'),
      { target: { value: 'Anjali Rao' } })
    fireEvent.change(within(dialog).getByLabelText('Date of birth they state'), { target: { value: dob } })
    fireEvent.change(within(dialog).getByLabelText('Last 4 digits of their phone'), { target: { value: '0000' } })
  }

  it('never fills in or prints the stored identifiers', async () => {
    const dialog = await openDialog()
    expect(within(dialog).getByLabelText('Their name, as they say it')).toHaveValue('')
    expect(within(dialog).getByLabelText('Date of birth they state')).toHaveValue('')
    expect(dialog.textContent).not.toContain(LEAKED_DOB)
    expect(dialog.textContent).not.toContain(LEAKED_PHONE)
    expect(within(dialog).getByRole('button', { name: 'Check' })).toBeDisabled()
  })

  it('shows only "no match" and keeps the visit closed', async () => {
    routeApi({ '/registration/identity-check': () => ({ result: 'no_match', checked: ['name'],
      checked_at: '2026-10-07T03:00:00Z' }) })
    const dialog = await openDialog()
    fillStated(dialog, '1970-01-01')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Check' }))
    expect(await within(dialog).findByText(/No match\. What the patient said does not match/))
      .toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Continue' })).toBeNull()
    expect(screen.queryByRole('form', { name: 'Register the visit' })).toBeNull()
    expect(dialog.textContent).not.toContain(LEAKED_DOB)
  })

  it('a match opens the visit, which is sent with what the patient stated', async () => {
    const dialog = await openDialog()
    fillStated(dialog)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Check' }))
    expect(await within(dialog).findByText('Match. This is the patient.')).toBeInTheDocument()
    expect(bodyOf('/registration/identity-check')).toEqual({
      patient_id: 'PAT-1', purpose: 'identity_confirm', name: 'Anjali Rao',
      date_of_birth: '1961-02-03', phone_last4: '0000' })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Continue' }))
    const form = await screen.findByRole('form', { name: 'Register the visit' })
    fireEvent.change(within(form).getByLabelText('Consulting doctor'), { target: { value: 'DOC-1' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Register visit and issue token' }))
    expect(await screen.findByText('Token 12')).toBeInTheDocument()
    const sent = bodyOf('/frontdesk/opd/visits')
    expect(sent.patient_id).toBe('PAT-1')
    expect(sent.department).toBeUndefined()
    expect(sent.doctor_id).toBe('DOC-1')
    expect(sent.identity_check).toEqual({ name: 'Anjali Rao', date_of_birth: '1961-02-03',
      phone_last4: '0000' })
    expect(String(sent.idempotency_key)).toMatch(/^opd-/)
  })

  it('a visit the server refuses as no match offers the check again', async () => {
    routeApi({ '/frontdesk/opd/visits': () => { throw refusal(409, {
      error: 'identity_not_confirmed',
      message: 'What the patient told you does not match this record.' }) } })
    const dialog = await openDialog()
    fillStated(dialog)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Check' }))
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Continue' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Register visit and issue token' }))
    expect(await screen.findByText(/does not match this record/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Check identity again' }))
    expect(screen.getByRole('dialog', { name: 'Confirm who the patient is' })).toBeInTheDocument()
  })

  it('asks an account with no department which clinic, from the roster', async () => {
    renderView({ scopedDepartment: null })
    await search()
    fireEvent.click(await screen.findByRole('button', { name: /Choose Anjali Rao/ }))
    const dialog = screen.getByRole('dialog')
    fillStated(dialog)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Check' }))
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Continue' }))
    const form = await screen.findByRole('form', { name: 'Register the visit' })
    const clinic = within(form).getByLabelText('Clinic')
    expect([...clinic.querySelectorAll('option')].map(o => o.textContent))
      .toEqual(['Choose a clinic…', 'Cardiology', 'Neurology'])
    expect(within(form).getByRole('button', { name: 'Register visit and issue token' })).toBeDisabled()
    fireEvent.change(clinic, { target: { value: 'Neurology' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Register visit and issue token' }))
    await screen.findByText('Token 12')
    expect(bodyOf('/frontdesk/opd/visits').department).toBe('Neurology')
  })
})

describe('a new patient', () => {
  async function openNewForm() {
    await search('Ravi Kumar')
    fireEvent.click(await screen.findByRole('button', { name: 'Register a new patient' }))
    return screen.getByRole('form', { name: 'Register a new patient' })
  }

  function fillNew(form: HTMLElement, { age }: { age?: string } = {}) {
    fireEvent.change(within(form).getByLabelText('Sex'), { target: { value: 'M' } })
    if (age) {
      fireEvent.click(within(form).getByLabelText('Stated age'))
      fireEvent.change(within(form).getByLabelText('Stated age in years'), { target: { value: age } })
    } else {
      fireEvent.change(within(form).getByLabelText('Their date of birth'), { target: { value: '1990-05-01' } })
    }
    fireEvent.change(within(form).getByLabelText('Hospital number (UHID)'), { target: { value: 'UH-2002' } })
  }

  it('starts with the typed name, has no allergy field, and hides ABHA with ABDM off', async () => {
    renderView()
    const form = await openNewForm()
    expect(within(form).getByLabelText('Full name')).toHaveValue('Ravi Kumar')
    expect(within(form).queryByLabelText(/allerg/i)).toBeNull()
    expect(within(form).queryByLabelText(/ABHA/)).toBeNull()
    expect(within(form).getByText(/Allergies are not recorded here/)).toBeInTheDocument()
  })

  it('labels the ABHA field unverified when ABDM is on', async () => {
    routeApi({ '/registration/config': () => ({ ...CONFIG, abdm_enabled: true }) })
    renderView()
    const form = await openNewForm()
    expect(within(form).getByLabelText('ABHA number (unverified)')).toBeInTheDocument()
    expect(within(form).getByText(/Recorded as unverified\. Recorded in Orb only/)).toBeInTheDocument()
  })

  it('shows the discharge link hint for a stated age, and sends the age as stated', async () => {
    routeApi({ '/registration/patients': () => ({
      patient: { patient_id: 'PAT-NEW', uhid: 'UH-2002' }, replayed: false, duplicate_review: null }) })
    renderView()
    const form = await openNewForm()
    fillNew(form, { age: '45' })
    expect(within(form).getByText(/No date of birth on file, so a secure discharge link cannot be issued/))
      .toBeInTheDocument()
    fireEvent.click(within(form).getByRole('button', { name: 'Register patient' }))
    const visitForm = await screen.findByRole('form', { name: 'Register the visit' })
    expect(within(visitForm).getByText('Registered, UHID UH-2002.')).toBeInTheDocument()
    expect(bodyOf('/registration/patients')).toEqual({ name: 'Ravi Kumar', sex: 'M', age_years: 45,
      uhid: 'UH-2002' })
    const call = apiFetch.mock.calls.find(([p]) => p === '/registration/patients')
    expect((call?.[1] as RequestInit).headers).toEqual({ 'Idempotency-Key': expect.stringMatching(/^reg-/) })
    fireEvent.click(within(visitForm).getByRole('button', { name: 'Register visit and issue token' }))
    await screen.findByText('Token 12')
    expect(bodyOf('/frontdesk/opd/visits').identity_check).toEqual({
      name: 'Ravi Kumar', age_years: 45, uhid: 'UH-2002' })
  })

  it('a possible duplicate needs a reason before registering past it', async () => {
    let attempts = 0
    routeApi({ '/registration/patients': () => {
      attempts += 1
      if (attempts === 1) {
        throw refusal(409, { error: 'duplicate_review_required', mode: 'warn',
          message: 'This may be a patient who is already registered. Check each record below.',
          candidates: [{ ...CARD, score: 60, band: 'possible', reasons: ['name_tokens'],
            reason_labels: ['Similar name'] }] })
      }
      return { patient: { patient_id: 'PAT-NEW', uhid: 'UH-2002' }, replayed: false,
        duplicate_review: { reason: 'x', candidate_ids: ['PAT-1'], check_incomplete: false } }
    } })
    renderView()
    const form = await openNewForm()
    fillNew(form)
    fireEvent.click(within(form).getByRole('button', { name: 'Register patient' }))
    expect(await within(form).findByText(/Possible match: Similar name/)).toBeInTheDocument()
    const registerAnyway = within(form).getByRole('button', { name: 'Register as a new patient' })
    expect(registerAnyway).toBeDisabled()
    fireEvent.change(within(form).getByLabelText(/Why is this a different person/), { target: { value: 'short' } })
    expect(registerAnyway).toBeDisabled()
    fireEvent.change(within(form).getByLabelText(/Why is this a different person/),
      { target: { value: 'Different father name and village' } })
    expect(registerAnyway).toBeEnabled()
    fireEvent.click(registerAnyway)
    await screen.findByRole('form', { name: 'Register the visit' })
    expect(bodyOf('/registration/patients').duplicate_review).toEqual({
      decision: 'register_new', candidate_ids: ['PAT-1'], reason: 'Different father name and village' })
    expect(document.body.textContent).not.toContain(LEAKED_DOB)
  })

  it('a probable duplicate offers the existing record and no override', async () => {
    routeApi({ '/registration/patients': () => { throw refusal(409, {
      error: 'probable_duplicate', mode: 'block',
      message: 'This looks like a patient who is already registered. Open their record.',
      candidates: [{ ...CARD, score: 85, band: 'probable', reasons: ['phone'],
        reason_labels: ['Same phone'] }] }) } })
    renderView()
    const form = await openNewForm()
    fillNew(form)
    fireEvent.click(within(form).getByRole('button', { name: 'Register patient' }))
    expect(await within(form).findByText(/Probable match: Same phone/)).toBeInTheDocument()
    expect(within(form).queryByRole('button', { name: 'Register as a new patient' })).toBeNull()
    fireEvent.click(within(form).getByRole('button', { name: 'Anjali Rao is the patient' }))
    expect(screen.getByRole('dialog', { name: 'Confirm who the patient is' })).toBeInTheDocument()
  })
})

describe('on the Outpatients tab', () => {
  it('offers the doctor first and reports the registered visit', async () => {
    const onVisitRegistered = vi.fn()
    renderView({ context: 'opd', defaultDoctorId: 'DOC-1', onVisitRegistered,
      holds: p => ['registration.read', 'registration.write', 'opd.consult'].includes(p) })
    expect(screen.getByText('Add a walk-in')).toBeInTheDocument()
    await search()
    fireEvent.click(await screen.findByRole('button', { name: /Choose Anjali Rao/ }))
    const dialog = screen.getByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText('Their name, as they say it'), { target: { value: 'Anjali Rao' } })
    fireEvent.click(within(dialog).getByLabelText('Age in years'))
    fireEvent.change(within(dialog).getByLabelText('Age they state, in years'), { target: { value: '64' } })
    fireEvent.click(within(dialog).getByLabelText('UHID'))
    fireEvent.change(within(dialog).getByLabelText('UHID they state'), { target: { value: 'UH-1001' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Check' }))
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Continue' }))
    const form = await screen.findByRole('form', { name: 'Register the visit' })
    await waitFor(() => expect(within(form).getByLabelText('Consulting doctor')).toHaveValue('DOC-1'))
    fireEvent.click(within(form).getByRole('button', { name: 'Register visit and issue token' }))
    await screen.findByText('Token 12')
    expect(onVisitRegistered).toHaveBeenCalledWith(expect.objectContaining({ id: 'OPD-9' }))
    expect(bodyOf('/frontdesk/opd/visits').identity_check).toEqual({
      name: 'Anjali Rao', age_years: 64, uhid: 'UH-1001' })
    expect(screen.queryByRole('button', { name: 'Open the OPD queue' })).toBeNull()
  })
})
