/**
 * The registration details drawer and the desk's prints (opd-6b).
 *
 *  1. A record outside the reader's scope reads only "Patient not found".
 *  2. A correction needs a reason and is sent against the row_version read,
 *     with only the fields that changed; a stale version shows the conflict
 *     and offers a reload.
 *  3. A reader let in by an outpatient visit sees the hidden fields as not
 *     shown, and cannot correct them.
 *  4. The UHID is offered for correction to the administrator only, who
 *     corrects nothing else.
 *  5. No full Aadhaar (or any full document number) field exists, and no
 *     allergy field.
 *  6. The change history lists each correction with its reason.
 *  7. Prints: the UHID card (name, number, Code 128 barcode, age and sex, the
 *     day registered; no date of birth) and the token slip, opened from the
 *     Register view after the visit is registered; the card is read from the
 *     server.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { RegistrationRecord, UhidCard } from '../../../api/registration'
import OpdTokenSlipPrint from '../../../components/print/OpdTokenSlipPrint'
import UhidCardPrint from '../../../components/print/UhidCardPrint'
import { formatClinicalDateTime } from '../../../lib/datetime'
import DemographicsDrawer from './DemographicsDrawer'
import RegisterPatientView from './RegisterPatientView'

const apiFetch = vi.fn()

vi.mock('../../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
  getUser: () => ({ role: 'receptionist' }),
}))

const DOB = '1961-02-03'

function record(extra: Partial<RegistrationRecord> = {}): RegistrationRecord {
  return {
    patient_id: 'PAT-1', uhid: 'UH-1001', name: 'Anjali Rao', sex: 'F', date_of_birth: DOB,
    age: 65, age_stated: false, age_recorded_on: null, phone: '9876500000',
    emergency_contact_name: 'Ravi Rao', emergency_contact_phone: '9876511111',
    address: '4 Temple Street, Mysuru', record_status: 'registered', inpatient_now: false,
    demographics: {
      guardian_relation: null, guardian_name: null, marital_status: 'married',
      preferred_language: 'Kannada', address_line: '4 Temple Street', city: 'Mysuru',
      district: 'Mysuru', state_code: 'KA', pin_code: '570001', alt_phone: null, email: null,
      id_document_type: 'aadhaar', id_document_last4: '4821', occupation: 'Teacher',
    },
    registration: { registered_at: '2026-10-07T03:00:00+00:00', registered_by_name: 'Desk One',
      registration_source: 'desk', uhid_source: 'hospital', updated_at: null, updated_by_name: null },
    row_version: 1, discharge_link_possible: true, date_of_birth_hint: null, abha: [],
    access: { arm: 'hospital_wide' },
    ...extra,
  }
}

const HISTORY = { patient_id: 'PAT-1', changes: [
  { id: 3, field: 'phone', old_value: '9876500001', new_value: '9876500000',
    reason: 'Patient gave a new number', changed_by: 'U-1', changed_by_name: 'Desk One',
    changed_at: '2026-10-08T05:00:00+00:00' },
] }

function refusal(status: number, detail: Record<string, unknown>) {
  const err = new Error(String(detail.message ?? 'refused')) as Error & { status: number; detail: unknown }
  err.status = status
  err.detail = detail
  return err
}

type Handler = (options?: RequestInit) => unknown
/** An override answers its path exactly, or any path that starts with it
 *  followed by '?' (a query). */
function routeApi(extra: Record<string, Handler> = {}) {
  apiFetch.mockImplementation(async (path: string, options?: RequestInit) => {
    for (const [prefix, fn] of Object.entries(extra)) {
      if (path === prefix || path.startsWith(`${prefix}?`)) return fn(options)
    }
    if (path === '/registration/patients/PAT-1/changes') return HISTORY
    if (path === '/registration/patients/PAT-1') return record()
    throw new Error(`unrouted ${path}`)
  })
}

function patches() {
  return apiFetch.mock.calls.filter(([p, o]) => p === '/registration/patients/PAT-1'
    && (o as RequestInit | undefined)?.method === 'PATCH')
}

const desk = (p: string) => ['registration.read', 'registration.write'].includes(p)
const adminHolds = (p: string) => ['registration.read'].includes(p)

let print: ReturnType<typeof vi.spyOn>
beforeEach(() => {
  apiFetch.mockReset()
  print = vi.spyOn(window, 'print').mockImplementation(() => {})
})
afterEach(() => {
  cleanup()
  print.mockRestore()
})

function openDrawer(holds = desk, isAdmin = false) {
  const onClose = vi.fn()
  render(<DemographicsDrawer patientId="PAT-1" patientName="Anjali Rao" onClose={onClose}
    holds={holds} isAdmin={isAdmin} />)
  return { onClose }
}

describe('reading the record', () => {
  it('a record outside the reader\'s scope reads only not found', async () => {
    routeApi({ '/registration/patients/PAT-1': () => {
      throw refusal(404, { error: 'patient_not_found', message: 'Patient not found' })
    } })
    openDrawer()
    expect(await screen.findByText('Patient not found.')).toBeInTheDocument()
    expect(screen.queryByText('UHID')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Correct details' })).toBeNull()
  })

  it('shows the record and its change history with the reason', async () => {
    routeApi()
    openDrawer()
    const dialog = await screen.findByRole('dialog', { name: 'Registration details for Anjali Rao' })
    expect(await within(dialog).findByText('UH-1001')).toBeInTheDocument()
    expect(within(dialog).getByText('Kannada')).toBeInTheDocument()
    expect(within(dialog).getByText('Aadhaar')).toBeInTheDocument()
    expect(within(dialog).getByText('4821')).toBeInTheDocument()
    const history = within(dialog).getByRole('region', { name: 'Change history' })
    expect(within(history).getByText('Phone: 9876500001 to 9876500000')).toBeInTheDocument()
    expect(within(history).getByText(
      `${formatClinicalDateTime('2026-10-08T05:00:00+00:00')}, Desk One. Reason: Patient gave a new number`))
      .toBeInTheDocument()
  })
})

describe('correcting the record', () => {
  it('needs a reason, and sends only what changed with the version read', async () => {
    routeApi({
      '/registration/patients/PAT-1': options => (options?.method === 'PATCH'
        ? { patient: record({ demographics: { ...record().demographics, city: 'Mandya' }, row_version: 2 }),
          changed_fields: ['city'] }
        : record()),
    })
    openDrawer()
    fireEvent.click(await screen.findByRole('button', { name: 'Correct details' }))
    const save = screen.getByRole('button', { name: 'Save correction' })
    expect(save).toBeDisabled()
    fireEvent.change(screen.getByLabelText('City or village'), { target: { value: 'Mandya' } })
    expect(save).toBeDisabled()
    fireEvent.change(screen.getByLabelText(/Why are these details being corrected/),
      { target: { value: 'short' } })
    expect(save).toBeDisabled()
    fireEvent.change(screen.getByLabelText(/Why are these details being corrected/),
      { target: { value: 'Patient moved house' } })
    expect(save).toBeEnabled()
    fireEvent.click(save)
    await waitFor(() => expect(patches()).toHaveLength(1))
    expect(JSON.parse(String((patches()[0][1] as RequestInit).body))).toEqual({
      row_version: 1, reason: 'Patient moved house', city: 'Mandya' })
    expect(await screen.findByText('Corrected: City or village.')).toBeInTheDocument()
  })

  it('a stale version shows the conflict and reloads', async () => {
    let reads = 0
    routeApi({
      '/registration/patients/PAT-1': options => {
        if (options?.method === 'PATCH') {
          throw refusal(409, { error: 'stale_row_version', current_row_version: 2,
            message: 'Someone changed this record after you opened it.' })
        }
        reads += 1
        return record({ row_version: reads })
      },
    })
    openDrawer()
    fireEvent.click(await screen.findByRole('button', { name: 'Correct details' }))
    fireEvent.change(screen.getByLabelText('Occupation'), { target: { value: 'Retired teacher' } })
    fireEvent.change(screen.getByLabelText(/Why are these details being corrected/),
      { target: { value: 'Retired this year' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save correction' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      /Someone changed this record after you opened it\. Reload it/)
    expect(screen.getByRole('button', { name: 'Save correction' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Reload the record' }))
    await waitFor(() => expect(reads).toBe(2))
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('switching to a stated age removes the date of birth in the same correction', async () => {
    routeApi({
      '/registration/patients/PAT-1': options => (options?.method === 'PATCH'
        ? { patient: record({ date_of_birth: null, age: 64, age_stated: true, row_version: 2 }),
          changed_fields: ['age_years', 'date_of_birth'] }
        : record()),
    })
    openDrawer()
    fireEvent.click(await screen.findByRole('button', { name: 'Correct details' }))
    fireEvent.click(screen.getByLabelText('Stated age'))
    fireEvent.change(screen.getByLabelText('Stated age in years'), { target: { value: '64' } })
    fireEvent.change(screen.getByLabelText(/Why are these details being corrected/),
      { target: { value: 'Date of birth was a guess' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save correction' }))
    await waitFor(() => expect(patches()).toHaveLength(1))
    expect(JSON.parse(String((patches()[0][1] as RequestInit).body))).toEqual({
      row_version: 1, reason: 'Date of birth was a guess', date_of_birth: null, age_years: 64 })
  })
})

describe('what a clinic and the administrator may do', () => {
  it('a reader let in by an outpatient visit sees hidden fields as not shown and cannot correct them', async () => {
    const hidden = ['date_of_birth', 'phone', 'emergency_contact_name', 'emergency_contact_phone',
      'address', 'guardian_relation', 'guardian_name', 'address_line', 'city', 'district',
      'state_code', 'pin_code', 'alt_phone', 'email', 'id_document_type', 'id_document_last4']
    routeApi({ '/registration/patients/PAT-1': () => record({
      date_of_birth: null, phone: null, emergency_contact_name: null, emergency_contact_phone: null,
      address: null, phone_last4: '0000', hidden_fields: hidden,
      demographics: { ...record().demographics, address_line: null, city: null, district: null,
        state_code: null, pin_code: null, id_document_type: null, id_document_last4: null },
      access: { arm: 'opd_visit', scope_until: '2026-10-14' } }) })
    openDrawer()
    fireEvent.click(await screen.findByRole('button', { name: 'Correct details' }))
    expect(screen.getAllByText('Not shown to your clinic').length).toBeGreaterThan(5)
    for (const label of ['Phone', 'City or village', 'PIN code', 'Identity document, last 4 characters',
      'Emergency contact']) {
      expect(screen.queryByLabelText(label)).toBeNull()
    }
    expect(screen.queryByLabelText('Date of birth')).toBeNull()
    expect(screen.getByLabelText('Occupation')).toBeInTheDocument()
    expect(screen.getByText('Phone ending 0000.')).toBeInTheDocument()
    expect(document.body.textContent).not.toContain(DOB)
  })

  it('offers the UHID for correction to the administrator only', async () => {
    routeApi()
    openDrawer(desk, false)
    fireEvent.click(await screen.findByRole('button', { name: 'Correct details' }))
    expect(screen.queryByLabelText('UHID (administrator only)')).toBeNull()
    cleanup()
    routeApi({
      '/registration/patients/PAT-1': options => (options?.method === 'PATCH'
        ? { patient: record({ uhid: 'UH-2002', row_version: 2 }), changed_fields: ['uhid'] }
        : record()),
    })
    openDrawer(adminHolds, true)
    fireEvent.click(await screen.findByRole('button', { name: 'Correct the UHID' }))
    expect(screen.queryByLabelText('Occupation')).toBeNull()
    expect(screen.queryByLabelText('City or village')).toBeNull()
    fireEvent.change(screen.getByLabelText('UHID (administrator only)'), { target: { value: 'UH-2002' } })
    fireEvent.change(screen.getByLabelText(/Why are these details being corrected/),
      { target: { value: 'Typed the wrong number' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save correction' }))
    await waitFor(() => expect(patches()).toHaveLength(1))
    expect(JSON.parse(String((patches()[0][1] as RequestInit).body))).toEqual({
      row_version: 1, reason: 'Typed the wrong number', uhid: 'UH-2002' })
  })

  it('has no full Aadhaar or document number field and no allergy field', async () => {
    routeApi()
    openDrawer()
    fireEvent.click(await screen.findByRole('button', { name: 'Correct details' }))
    const last4 = screen.getByLabelText('Identity document, last 4 characters')
    expect(last4).toHaveAttribute('maxLength', '4')
    const labels = Array.from(document.querySelectorAll('label, legend')).map(l => l.textContent ?? '')
    for (const text of labels) {
      expect(text).not.toMatch(/aadhaar number|full number|document number|allerg/i)
    }
    expect(Array.from(document.querySelectorAll('input')).every(i => !i.maxLength || i.maxLength <= 200))
      .toBe(true)
  })
})

describe('the prints', () => {
  const CARD: UhidCard = { patient_id: 'PAT-1', uhid: 'UH-1001', name: 'Anjali Rao', sex: 'F', age: 65,
    age_stated: false, registered_on: '2026-10-07', uhid_source: 'hospital' }

  it('the UHID card carries the name, number, barcode, age and sex, and no date of birth', () => {
    render(<UhidCardPrint card={CARD} onAfterPrint={() => {}} autoPrint={false} />)
    const card = screen.getByTestId('uhid-card-print')
    expect(Array.from(card.querySelectorAll('section > div')).map(d => d.textContent)).toEqual([
      'Anjali Rao', '65 y, Female', 'UHID UH-1001', 'Registered 7 Oct 2026'])
    const svg = within(card).getByRole('img', { name: 'Barcode for UHID UH-1001' })
    expect(svg.querySelectorAll('rect').length).toBeGreaterThan(10)
    expect(card.textContent).not.toContain(DOB)
  })

  it('the token slip carries the token, clinic, doctor, patient and day', () => {
    render(<OpdTokenSlipPrint autoPrint={false} onAfterPrint={() => {}} visit={{
      token_number: 12, department: 'Cardiology', doctor_name: 'Dr Iyer', visit_date: '2026-10-07',
      registered_at: '2026-10-07T03:00:00+00:00', patient_name: 'Anjali Rao', patient_mrn: 'UH-1001',
      room: '3' }} />)
    const slip = screen.getByTestId('opd-token-slip-print')
    const lines = Array.from(slip.querySelectorAll('section > div')).map(d => d.textContent)
    expect(lines.slice(0, 5)).toEqual(['Outpatient token', '12', 'Cardiology', 'Doctor: Dr Iyer', 'Room 3'])
    expect(lines.slice(5, 7)).toEqual(['Anjali Rao', 'UHID UH-1001'])
    expect(lines[7]).toMatch(/^7 Oct 2026, registered \d{2}:\d{2} [AP]M$/)
  })

  it('the Register view offers the slip, the card and the details once the visit is registered', async () => {
    const SEARCH_CARD = { patient_id: 'PAT-1', uhid: 'UH-1001', name: 'Anjali Rao', sex: 'F', age: 65,
      age_stated: false, phone_last4: '0000', last_visit_date: null, inpatient_now: false }
    routeApi({
      '/registration/config': () => ({ uhid_source: 'hospital', uhid_ready: true, duplicate_mode: 'block',
        abdm_enabled: false, abha_notice: '', search_min_chars: 3, search_max_results: 20,
        reason_min_chars: 8 }),
      '/frontdesk/opd/doctors': () => ({ doctors: [{ id: 'DOC-1', name: 'Dr Iyer', department: 'Cardiology' }] }),
      '/registration/search': () => ({ results: [SEARCH_CARD], truncated: false, kinds: ['name'],
        min_chars: 3, max_results: 20 }),
      '/registration/identity-check': () => ({ result: 'match', checked: ['name', 'date_of_birth', 'phone_last4'],
        checked_at: '2026-10-07T03:00:00Z' }),
      '/frontdesk/opd/visits': () => ({ id: 'OPD-9', patient_id: 'PAT-1', patient_name: 'Anjali Rao',
        patient_mrn: 'UH-1001', patient_age: 65, patient_sex: 'F', department: 'Cardiology',
        doctor_id: 'DOC-1', doctor_name: 'Dr Iyer', visit_date: '2026-10-07', token_number: 12,
        visit_type: 'new', chief_complaint: null, status: 'waiting',
        registered_at: '2026-10-07T03:00:00+00:00', called_at: null, completed_at: null,
        cancelled_at: null, cancel_reason: null, consultation_charge_id: null, notes: null,
        waiting_minutes: 0, consultation_minutes: null }),
      '/registration/patients/PAT-1/card': () => CARD,
    })
    render(<RegisterPatientView scopedDepartment="Cardiology" openView={vi.fn()} onFeatureOff={vi.fn()}
      holds={desk} />)
    fireEvent.change(screen.getByLabelText('UHID, full phone number or name'), { target: { value: 'Anjali' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    fireEvent.click(await screen.findByRole('button', { name: /Choose Anjali Rao/ }))
    const dialog = screen.getByRole('dialog', { name: 'Confirm who the patient is' })
    fireEvent.change(within(dialog).getByLabelText('Their name, as they say it'), { target: { value: 'Anjali Rao' } })
    fireEvent.change(within(dialog).getByLabelText('Date of birth they state'), { target: { value: DOB } })
    fireEvent.change(within(dialog).getByLabelText('Last 4 digits of their phone'), { target: { value: '0000' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Check' }))
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Continue' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Register visit and issue token' }))
    expect(await screen.findByText('Token 12')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Print token slip' }))
    const slip = await screen.findByTestId('opd-token-slip-print')
    expect(slip.parentElement).toBe(document.body)
    expect(within(slip).getByText('12')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Print UHID card' }))
    const card = await screen.findByTestId('uhid-card-print')
    expect(card.parentElement).toBe(document.body)
    expect(apiFetch.mock.calls.some(([p]) => p === '/registration/patients/PAT-1/card')).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Registration details' }))
    expect(await screen.findByRole('dialog', { name: 'Registration details for Anjali Rao' })).toBeInTheDocument()
  })
})
