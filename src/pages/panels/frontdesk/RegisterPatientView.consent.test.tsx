/**
 * The privacy notice and consent step of a new registration (opd-19).
 *
 *  1. An account holding consent.record (the doctor's walk-in) is offered the
 *     step and must choose: record it now, or not now. Registering waits for
 *     the choice and, for "now", for both facts ticked, the notice version,
 *     the language and how consent was given.
 *  2. The notice language follows the patient's preferred language when the
 *     notice may be given in it; the preferred language is sent as typed.
 *  3. "Not now" sends no consent; a guardian's consent needs their name and
 *     relationship.
 *  4. The receptionist (no consent.record until counsel agrees, ruling
 *     OPD-N3) is not offered the step and is told a doctor records consent;
 *     her registration carries no consent.
 *  5. A server that does not describe the step offers nothing and says
 *     nothing about consent.
 *  6. The note after registering repeats what the SERVER recorded.
 *  7. Who gave consent fits how and the patient's age, as the server checks:
 *     a guardian method is never given by the patient, and a patient under
 *     the adult age (by date of birth or stated age) does not consent for
 *     themselves. A line says what to change; registering waits.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import RegisterPatientView from './RegisterPatientView'
import {
  consentInput,
  consentReady,
  emptyConsentDraft,
  givenByProblem,
  matchNoticeLanguage,
  underAdultAge,
} from './registrationConsent'

const apiFetch = vi.fn()

vi.mock('../../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
}))

const DEPT = 'Cardiology'

const CONSENT_OPTIONS = {
  purpose: 'treatment',
  purpose_label: 'Care and treatment in this hospital, including the records it generates',
  methods: [
    { value: 'written_signature', label: 'Signed paper form, scanned or filed' },
    { value: 'verbal_witnessed', label: 'Spoken, with a second member of staff witnessing' },
    { value: 'proxy_guardian', label: 'Given by a guardian for a child or a person with disability' },
  ],
  given_by: ['patient', 'guardian', 'proxy'],
  guardian_methods: ['proxy_guardian'],
  adult_age: 18,
  notice_languages: ['English', 'Hindi', 'Tamil'],
  notice_version_max: 40,
  retention_text: 'Registration details (name, age, contact details and address) are part of the '
    + 'medical record.',
}

const CONFIG = {
  uhid_source: 'hospital', uhid_pattern_set: false, uhid_pattern_valid: true,
  uhid_series_configured: false, uhid_ready: true, duplicate_mode: 'warn',
  abdm_enabled: false, abha_notice: 'Recorded in Orb only.',
  search_min_chars: 3, search_max_results: 20, reason_min_chars: 8,
  thresholds: { probable: 70, possible: 50 }, id_document_types: [], allergies_at_desk: false,
  consent: CONSENT_OPTIONS,
}

const ROSTER = { doctors: [{ id: 'DOC-1', name: 'Dr Iyer', department: DEPT }] }

const doctorHolds = (p: string) =>
  ['registration.read', 'registration.write', 'opd.consult', 'consent.record'].includes(p)
const receptionistHolds = (p: string) => ['registration.read', 'registration.write'].includes(p)

let registerReply: Record<string, unknown>

function routeApi(config: Record<string, unknown> = CONFIG) {
  apiFetch.mockImplementation(async (path: string) => {
    if (path === '/registration/config') return config
    if (path === '/frontdesk/opd/doctors') return ROSTER
    if (path.startsWith('/registration/search')) {
      return { results: [], truncated: false, kinds: ['name'], min_chars: 3, max_results: 20 }
    }
    if (path === '/registration/patients') return registerReply
    throw new Error(`unrouted ${path}`)
  })
}

function bodyOf(path: string): Record<string, unknown> {
  const call = [...apiFetch.mock.calls].reverse().find(([p]) => p === path)
  if (!call) throw new Error(`no call to ${path}`)
  return JSON.parse(String((call[1] as RequestInit).body))
}

function renderView(holds = doctorHolds) {
  render(<RegisterPatientView scopedDepartment={DEPT} openView={vi.fn()} onFeatureOff={vi.fn()}
    holds={holds} context="opd" />)
}

async function openNewForm() {
  fireEvent.change(screen.getByLabelText('UHID, full phone number or name'),
    { target: { value: 'Meena Pillai' } })
  fireEvent.click(screen.getByRole('button', { name: 'Search' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Register a new patient' }))
  return screen.getByRole('form', { name: 'Register a new patient' })
}

function fillPatient(form: HTMLElement, language = '', dateOfBirth = '1980-03-04') {
  fireEvent.change(within(form).getByLabelText('Sex'), { target: { value: 'F' } })
  fireEvent.change(within(form).getByLabelText('Their date of birth'), { target: { value: dateOfBirth } })
  fireEvent.change(within(form).getByLabelText('Hospital number (UHID)'), { target: { value: 'UH-3003' } })
  if (language) {
    fireEvent.change(within(form).getByLabelText('Preferred language'), { target: { value: language } })
  }
}

beforeEach(() => {
  apiFetch.mockReset()
  try { window.localStorage.clear() } catch { /* storage may be absent */ }
  registerReply = { patient: { patient_id: 'PAT-NEW', uhid: 'UH-3003' }, replayed: false,
    duplicate_review: null }
  routeApi()
})
afterEach(() => cleanup())

describe('the consent step for an account that records consent', () => {
  it('waits for a choice, and records the notice and consent in the preferred language', async () => {
    registerReply = { ...registerReply, consents: [{
      id: 'CNS-1', purpose: 'treatment', purpose_label: CONSENT_OPTIONS.purpose_label,
      notice_version: 'DPN-2026-1', notice_language: 'Hindi', method: 'written_signature',
      method_label: 'Signed paper form, scanned or filed', given_by: 'patient',
      granted_at: '2026-10-10T04:00:00+00:00', effective_status: 'active' }] }
    renderView()
    const form = await openNewForm()
    fillPatient(form, 'hindi')
    const step = within(form).getByRole('group', { name: 'Privacy notice and consent' })
    expect(within(step).getByText(/part of the medical record/)).toBeInTheDocument()
    const register = within(form).getByRole('button', { name: 'Register patient' })
    expect(register).toBeDisabled()

    fireEvent.click(within(step).getByLabelText('Record the notice and consent now'))
    expect(register).toBeDisabled()
    fireEvent.change(within(step).getByLabelText('Notice version (printed on the notice)'),
      { target: { value: 'DPN-2026-1' } })
    expect(within(step).getByLabelText('Language of the notice')).toHaveValue('Hindi')
    fireEvent.click(within(step).getByLabelText('The patient was given the privacy notice in Hindi.'))
    expect(register).toBeDisabled()
    fireEvent.click(within(step).getByLabelText(/The patient agrees to: care and treatment/))
    expect(register).toBeDisabled()
    fireEvent.change(within(step).getByLabelText('How consent was given'),
      { target: { value: 'written_signature' } })
    expect(register).toBeEnabled()

    fireEvent.click(register)
    const visitForm = await screen.findByRole('form', { name: 'Register the visit' })
    expect(bodyOf('/registration/patients')).toEqual({
      name: 'Meena Pillai', sex: 'F', date_of_birth: '1980-03-04', uhid: 'UH-3003',
      preferred_language: 'hindi',
      consent: { notice_acknowledged: true, treatment_consent: true, notice_version: 'DPN-2026-1',
        notice_language: 'Hindi', method: 'written_signature', given_by: 'patient' },
    })
    expect(within(visitForm).getByText(
      'Consent to treatment recorded, with notice version DPN-2026-1 in Hindi.')).toBeInTheDocument()
    // The version is remembered on this device for the next registration.
    expect(window.localStorage.getItem('orb.registration.noticeVersion')).toBe('DPN-2026-1')
  })

  it('"Not now" registers without any consent', async () => {
    renderView()
    const form = await openNewForm()
    fillPatient(form)
    const step = within(form).getByRole('group', { name: 'Privacy notice and consent' })
    fireEvent.click(within(step).getByLabelText('Not now'))
    fireEvent.click(within(form).getByRole('button', { name: 'Register patient' }))
    const visitForm = await screen.findByRole('form', { name: 'Register the visit' })
    expect(bodyOf('/registration/patients')).toEqual({
      name: 'Meena Pillai', sex: 'F', date_of_birth: '1980-03-04', uhid: 'UH-3003' })
    expect(within(visitForm).queryByText(/Consent to treatment recorded/)).toBeNull()
  })

  it('a guardian\'s consent needs their name and relationship', async () => {
    renderView()
    const form = await openNewForm()
    fillPatient(form)
    const step = within(form).getByRole('group', { name: 'Privacy notice and consent' })
    fireEvent.click(within(step).getByLabelText('Record the notice and consent now'))
    fireEvent.change(within(step).getByLabelText('Notice version (printed on the notice)'),
      { target: { value: 'DPN-2026-1' } })
    fireEvent.change(within(step).getByLabelText('Language of the notice'), { target: { value: 'Tamil' } })
    fireEvent.click(within(step).getByLabelText('The patient was given the privacy notice in Tamil.'))
    fireEvent.click(within(step).getByLabelText(/The patient agrees to/))
    fireEvent.change(within(step).getByLabelText('How consent was given'),
      { target: { value: 'verbal_witnessed' } })
    fireEvent.click(within(step).getByLabelText('A guardian'))
    const register = within(form).getByRole('button', { name: 'Register patient' })
    expect(register).toBeDisabled()
    fireEvent.change(within(step).getByLabelText('Their name'), { target: { value: 'Suresh Pillai' } })
    expect(register).toBeDisabled()
    fireEvent.change(within(step).getByLabelText('Relationship to the patient'),
      { target: { value: 'Father' } })
    expect(register).toBeEnabled()
    fireEvent.click(register)
    await screen.findByRole('form', { name: 'Register the visit' })
    expect(bodyOf('/registration/patients').consent).toEqual({
      notice_acknowledged: true, treatment_consent: true, notice_version: 'DPN-2026-1',
      notice_language: 'Tamil', method: 'verbal_witnessed', given_by: 'guardian',
      proxy_name: 'Suresh Pillai', proxy_relationship: 'Father' })
  })
})

function fillConsentStep(step: HTMLElement, method: string) {
  fireEvent.click(within(step).getByLabelText('Record the notice and consent now'))
  fireEvent.change(within(step).getByLabelText('Notice version (printed on the notice)'),
    { target: { value: 'DPN-2026-1' } })
  fireEvent.change(within(step).getByLabelText('Language of the notice'), { target: { value: 'Tamil' } })
  fireEvent.click(within(step).getByLabelText('The patient was given the privacy notice in Tamil.'))
  fireEvent.click(within(step).getByLabelText(/The patient agrees to/))
  fireEvent.change(within(step).getByLabelText('How consent was given'), { target: { value: method } })
}

const CHILD_LINE = 'This patient is under 18, so a parent or guardian gives consent for them. '
  + 'Choose who gave consent and record their name.'
const GUARDIAN_METHOD_LINE = 'This way of giving consent means a guardian or someone acting for '
  + 'the patient gave it. Choose who gave consent and record their name.'

describe('who gave consent fits how and the patient\'s age', () => {
  it('a child does not consent for themselves; a named guardian does', async () => {
    renderView()
    const form = await openNewForm()
    fillPatient(form, '', '2020-01-01')
    const step = within(form).getByRole('group', { name: 'Privacy notice and consent' })
    fillConsentStep(step, 'written_signature')
    const register = within(form).getByRole('button', { name: 'Register patient' })
    expect(register).toBeDisabled()
    expect(within(step).getByText(CHILD_LINE)).toBeInTheDocument()

    fireEvent.click(within(step).getByLabelText('A guardian'))
    expect(within(step).queryByText(CHILD_LINE)).toBeNull()
    fireEvent.change(within(step).getByLabelText('Their name'), { target: { value: 'Suresh Pillai' } })
    fireEvent.change(within(step).getByLabelText('Relationship to the patient'),
      { target: { value: 'Father' } })
    expect(register).toBeEnabled()
    fireEvent.click(register)
    await screen.findByRole('form', { name: 'Register the visit' })
    const body = bodyOf('/registration/patients')
    expect(body.date_of_birth).toBe('2020-01-01')
    expect(body.consent).toMatchObject({ given_by: 'guardian', proxy_name: 'Suresh Pillai',
      proxy_relationship: 'Father', method: 'written_signature' })
  })

  it('a stated age under 18 asks for a guardian, and an adult age does not', async () => {
    renderView()
    const form = await openNewForm()
    fillPatient(form)
    fireEvent.click(within(form).getByLabelText('Stated age'))
    fireEvent.change(within(form).getByLabelText('Stated age in years'), { target: { value: '12' } })
    const step = within(form).getByRole('group', { name: 'Privacy notice and consent' })
    fillConsentStep(step, 'verbal_witnessed')
    const register = within(form).getByRole('button', { name: 'Register patient' })
    expect(register).toBeDisabled()
    expect(within(step).getByText(CHILD_LINE)).toBeInTheDocument()
    fireEvent.change(within(form).getByLabelText('Stated age in years'), { target: { value: '18' } })
    expect(within(step).queryByText(CHILD_LINE)).toBeNull()
    expect(register).toBeEnabled()
  })

  it('a guardian method is never given by the patient', async () => {
    renderView()
    const form = await openNewForm()
    fillPatient(form)
    const step = within(form).getByRole('group', { name: 'Privacy notice and consent' })
    fillConsentStep(step, 'proxy_guardian')
    const register = within(form).getByRole('button', { name: 'Register patient' })
    expect(register).toBeDisabled()
    expect(within(step).getByText(GUARDIAN_METHOD_LINE)).toBeInTheDocument()

    fireEvent.click(within(step).getByLabelText('Someone acting for them'))
    expect(within(step).queryByText(GUARDIAN_METHOD_LINE)).toBeNull()
    expect(register).toBeDisabled()
    fireEvent.change(within(step).getByLabelText('Their name'), { target: { value: 'Anil Rao' } })
    fireEvent.change(within(step).getByLabelText('Relationship to the patient'),
      { target: { value: 'Brother' } })
    expect(register).toBeEnabled()
    fireEvent.click(register)
    await screen.findByRole('form', { name: 'Register the visit' })
    expect(bodyOf('/registration/patients').consent).toMatchObject({
      method: 'proxy_guardian', given_by: 'proxy', proxy_name: 'Anil Rao',
      proxy_relationship: 'Brother' })
  })
})

describe('an account that does not record consent', () => {
  it('the receptionist is not offered the step, is told who records it, and sends none', async () => {
    renderView(receptionistHolds)
    const form = await openNewForm()
    fillPatient(form, 'English')
    expect(within(form).queryByRole('group', { name: 'Privacy notice and consent' })).toBeNull()
    expect(within(form).getByText(/Consent is not recorded at this desk/)).toBeInTheDocument()
    fireEvent.click(within(form).getByRole('button', { name: 'Register patient' }))
    await screen.findByRole('form', { name: 'Register the visit' })
    const body = bodyOf('/registration/patients')
    expect(body.consent).toBeUndefined()
    expect(body.preferred_language).toBe('English')
  })

  it('a server that does not describe the step offers nothing about consent', async () => {
    const { consent: _gone, ...older } = CONFIG
    routeApi(older)
    renderView()
    const form = await openNewForm()
    await waitFor(() => expect(within(form).getByLabelText('Preferred language')).toBeInTheDocument())
    expect(within(form).queryByRole('group', { name: 'Privacy notice and consent' })).toBeNull()
    expect(within(form).queryByText(/Consent is not recorded/)).toBeNull()
    fillPatient(form)
    expect(within(form).getByRole('button', { name: 'Register patient' })).toBeEnabled()
  })
})

describe('the draft rules', () => {
  it('matches a notice language whatever its case and spacing, and nothing else', () => {
    expect(matchNoticeLanguage(CONSENT_OPTIONS, '  hINDI ')).toBe('Hindi')
    expect(matchNoticeLanguage(CONSENT_OPTIONS, 'Tulu')).toBe('')
    expect(matchNoticeLanguage(CONSENT_OPTIONS, '')).toBe('')
  })

  it('an undecided draft is not ready and sends nothing', () => {
    const draft = emptyConsentDraft()
    expect(consentReady(CONSENT_OPTIONS, draft, 'Hindi', false)).toBe(false)
    expect(consentInput(CONSENT_OPTIONS, draft, 'Hindi')).toBeUndefined()
  })

  it('a version longer than the server allows is not ready', () => {
    const draft = { ...emptyConsentDraft(), choice: 'now' as const, noticeGiven: true,
      treatmentConsent: true, noticeVersion: 'v'.repeat(41), method: 'written_signature' }
    expect(consentReady(CONSENT_OPTIONS, draft, 'Hindi', false)).toBe(false)
    expect(consentReady(CONSENT_OPTIONS, { ...draft, noticeVersion: 'v1' }, 'Hindi', false)).toBe(true)
    // A preferred language the notice cannot be given in needs a chosen one.
    expect(consentReady(CONSENT_OPTIONS, { ...draft, noticeVersion: 'v1' }, 'Tulu', false)).toBe(false)
  })

  it('counts the adult age from a date of birth or a stated age, and knows when it cannot', () => {
    const now = new Date(2026, 9, 10, 12, 0)
    expect(underAdultAge(CONSENT_OPTIONS, { dateOfBirth: '2008-10-10' }, now)).toBe(false)
    expect(underAdultAge(CONSENT_OPTIONS, { dateOfBirth: '2008-10-11' }, now)).toBe(true)
    expect(underAdultAge(CONSENT_OPTIONS, { dateOfBirth: '1980-03-04' }, now)).toBe(false)
    expect(underAdultAge(CONSENT_OPTIONS, { ageYears: 17 }, now)).toBe(true)
    expect(underAdultAge(CONSENT_OPTIONS, { ageYears: 18 }, now)).toBe(false)
    expect(underAdultAge(CONSENT_OPTIONS, { ageYears: Number.NaN }, now)).toBeNull()
    expect(underAdultAge(CONSENT_OPTIONS, { dateOfBirth: '' }, now)).toBeNull()
    expect(underAdultAge(CONSENT_OPTIONS, { dateOfBirth: '10/10/2008' }, now)).toBeNull()
  })

  it('the patient gives consent only as an adult and never by a guardian method', () => {
    const ready = { ...emptyConsentDraft(), choice: 'now' as const, noticeGiven: true,
      treatmentConsent: true, noticeVersion: 'v1', method: 'written_signature' }
    expect(consentReady(CONSENT_OPTIONS, ready, 'Hindi', false)).toBe(true)
    expect(consentReady(CONSENT_OPTIONS, ready, 'Hindi', true)).toBe(false)
    expect(consentReady(CONSENT_OPTIONS, ready, 'Hindi', null)).toBe(false)
    expect(givenByProblem(CONSENT_OPTIONS, ready, null)).toBe(
      'Record the date of birth or stated age first.')
    const guardianMethod = { ...ready, method: 'proxy_guardian' }
    expect(consentReady(CONSENT_OPTIONS, guardianMethod, 'Hindi', false)).toBe(false)
    expect(givenByProblem(CONSENT_OPTIONS, guardianMethod, false)).toBe(GUARDIAN_METHOD_LINE)
    const named = { ...guardianMethod, givenBy: 'guardian', proxyName: 'Ramesh Patil',
      proxyRelationship: 'Father' }
    expect(givenByProblem(CONSENT_OPTIONS, named, true)).toBe('')
    expect(consentReady(CONSENT_OPTIONS, named, 'Hindi', true)).toBe(true)
    expect(consentInput(CONSENT_OPTIONS, named, 'Hindi')).toMatchObject({
      method: 'proxy_guardian', given_by: 'guardian', proxy_name: 'Ramesh Patil' })
  })
})
