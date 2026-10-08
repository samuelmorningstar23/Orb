/**
 * Pharmacy store, Masters: drug identity (grn-6, ruling GRN-RS1 option (b)).
 *
 *  1. 'Add a drug' saves only an identity the server has previewed: the
 *     screening answer (drug classes, interaction check) is shown first, a
 *     changed field needs a fresh check, and a drug with the same generic
 *     name, strength and form needs the 'another brand' tick.
 *  2. The approve tier is not offered 'Add a drug'.
 *  3. A draft in the drug list says it is waiting for a second pharmacist.
 *  4. The pharmacist who asked may only withdraw; another pharmacist
 *     confirms, sending back exactly the identity shown; refusing needs a
 *     reason.
 *  5. Retiring a drug needs a reason; a correction is sent for checking, and
 *     a same-identity refusal offers the 'another brand' tick.
 *
 * Driven through a mocked apiFetch, so the real src/api/pharmacyStore files
 * build the URLs and bodies.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import MastersView from './MastersView'

const apiFetch = vi.fn()
let role = 'pharmacist'
let userId = 'PHA1'

vi.mock('../../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
  errorDetailMessage: (_d: unknown, status: number) => `Request failed (${status})`,
  handleResponseError: async () => { throw new Error('x') },
  getUser: () => ({ id: userId, name: 'Test user', role }),
}))

const OPTIONS = {
  drug_schedules: [{ value: 'none', label: 'No schedule' }, { value: 'H', label: 'Schedule H' }],
  ndps_categories: [{ value: 'none', label: 'Not controlled' }, { value: 'narcotic', label: 'Narcotic' }],
  storage_conditions: [{ value: 'room_temperature', label: 'Room temperature' }],
  store_kinds: [],
  not_classified: 'Not classified',
  not_configured: 'Not configured',
}

function drug(overrides: Record<string, unknown> = {}) {
  return {
    id: 'MED-PARA', name: 'Paracetamol 650mg Oral Tablet', generic_name: 'Paracetamol',
    form: 'Tablet', strength: '650mg', pack_size_units: null, purchase_uom: null,
    dispense_unit: null, hsn_code: null, gst_rate_bp: null, manufacturer: null,
    drug_schedule: null, ndps_category: null, is_essential_narcotic: 0, storage_condition: null,
    reorder_level_units: null, reorder_qty_units: null, lead_time_days: null, is_active: 1,
    identity_status: 'active', identity_label: 'Confirmed for ordering', orderable_identity: true,
    updated_by: null, updated_at: null, schedule_label: 'Not classified',
    ndps_label: 'Not classified', storage_label: 'Not configured', gst_label: 'Not configured',
    pending_requests: 0,
    ...overrides,
  }
}

const NEW_IDENTITY = { name: 'Novamox 500 mg Capsule', generic_name: 'Amoxicillin',
  strength: '500 mg', form: 'Capsule' }

const PREVIEW = {
  identity: NEW_IDENTITY,
  screening: { screened_as: 'Amoxicillin', classes: ['penicillins'], class_labels: ['penicillins'],
    interaction_recognised: true },
  same_name: [],
  same_identity: [{ id: 'MED-MOX-500', name: 'Mox 500mg Capsule', generic_name: 'Amoxicillin',
    form: 'Capsule', strength: '500mg', identity_status: 'active' }],
}

const IDENTITY_REQUEST = {
  id: 41, helix_medication_id: 'MED-NOVA', change_kind: 'identity_create', field: 'identity',
  from_value: null, to_value: JSON.stringify(NEW_IDENTITY), reason: 'New brand from the vendor',
  requested_by: 'PHA1', requested_at: '2026-10-09 05:00:00', decision: 'pending',
  decided_by: null, decided_at: null, decision_reason: null, drug_name: NEW_IDENTITY.name,
  requested_by_name: 'Pharmacist One', identity: NEW_IDENTITY, previous_identity: null,
  screening: PREVIEW.screening, requested_by_loader: false, drug_identity_status: 'draft',
}

type Handler = (path: string, init?: RequestInit) => unknown

function routeApi(overrides: Record<string, Handler> = {}, drugs = [drug()]) {
  apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
    for (const [prefix, handler] of Object.entries(overrides)) {
      if (path.startsWith(prefix)) return handler(path, init)
    }
    if (path.startsWith('/pharmacy-store/drugs')) return { drugs, options: OPTIONS }
    if (path.startsWith('/pharmacy-store/identity-requests')) return { requests: [IDENTITY_REQUEST] }
    if (path.startsWith('/pharmacy-store/classification-requests')) return { requests: [] }
    throw new Error(`unexpected ${path}`)
  })
}

function calls(prefix: string, method = 'POST') {
  return apiFetch.mock.calls.filter(([path, init]) =>
    String(path).startsWith(prefix) && (init as RequestInit | undefined)?.method === method)
}

function body(call: unknown[]) {
  return JSON.parse(String((call[1] as RequestInit).body))
}

function renderView() {
  const onModuleOff = vi.fn()
  render(<MastersView meta={{}} openView={() => {}} onModuleOff={onModuleOff} />)
  return { onModuleOff }
}

function typeIdentity(scope: ReturnType<typeof within>, identity: Record<string, string>) {
  fireEvent.change(scope.getByLabelText('Name on the pack'), { target: { value: identity.name } })
  fireEvent.change(scope.getByLabelText('Generic name'), { target: { value: identity.generic_name } })
  fireEvent.change(scope.getByLabelText('Strength'), { target: { value: identity.strength } })
  fireEvent.change(scope.getByLabelText('Form'), { target: { value: identity.form } })
}

beforeEach(() => {
  apiFetch.mockReset()
  role = 'pharmacist'
  userId = 'PHA1'
})

afterEach(() => { vi.restoreAllMocks() })

describe('MastersView drug identity', () => {
  it('saves only a previewed identity, and asks about another brand', async () => {
    routeApi({
      '/pharmacy-store/drugs/preview': () => PREVIEW,
      '/pharmacy-store/drugs': (path, init) => (init?.method === 'POST'
        ? { drug: drug({ id: 'MED-NOVA', ...NEW_IDENTITY, identity_status: 'draft' }),
          request: IDENTITY_REQUEST, screening: PREVIEW.screening }
        : { drugs: [drug()], options: OPTIONS }),
    })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Add a drug' }))
    const form = within(screen.getByRole('form', { name: 'Add a drug' }))
    typeIdentity(form, NEW_IDENTITY)
    const save = form.getByRole('button', { name: 'Save as draft' })
    expect(save).toBeDisabled()

    fireEvent.click(form.getByRole('button', { name: 'Check screening' }))
    expect(await form.findByText('Screened as Amoxicillin.')).toBeInTheDocument()
    expect(form.getByText('The allergy check reads it as: penicillins.')).toBeInTheDocument()
    expect(form.getByText('The interaction check knows this drug.')).toBeInTheDocument()
    expect(form.getByText(/Same generic name, strength and form as: Mox 500mg Capsule/))
      .toBeInTheDocument()
    expect(body(calls('/pharmacy-store/drugs/preview')[0])).toEqual(NEW_IDENTITY)

    fireEvent.click(save)
    expect(await form.findByText('Say whether this is another brand of the same medicine.'))
      .toBeInTheDocument()
    expect(calls('/pharmacy-store/drugs').filter(c => c[0] === '/pharmacy-store/drugs')).toHaveLength(0)

    fireEvent.click(form.getByLabelText('This is another brand of the same medicine'))
    fireEvent.click(save)
    await waitFor(() => expect(calls('/pharmacy-store/drugs')
      .filter(c => c[0] === '/pharmacy-store/drugs')).toHaveLength(1))
    const sent = body(calls('/pharmacy-store/drugs').filter(c => c[0] === '/pharmacy-store/drugs')[0])
    expect(sent).toEqual({ ...NEW_IDENTITY, another_brand: true })
    expect(await screen.findByText(/was saved as a draft\. Another pharmacist must confirm it/))
      .toBeInTheDocument()
  })

  it('needs a fresh check after the identity changes', async () => {
    routeApi({ '/pharmacy-store/drugs/preview': () => ({ ...PREVIEW, same_identity: [] }) })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Add a drug' }))
    const form = within(screen.getByRole('form', { name: 'Add a drug' }))
    typeIdentity(form, NEW_IDENTITY)
    fireEvent.click(form.getByRole('button', { name: 'Check screening' }))
    await form.findByText('Screened as Amoxicillin.')
    expect(form.getByRole('button', { name: 'Save as draft' })).toBeEnabled()
    fireEvent.change(form.getByLabelText('Strength'), { target: { value: '250 mg' } })
    expect(form.getByRole('button', { name: 'Save as draft' })).toBeDisabled()
    expect(form.queryByText('Screened as Amoxicillin.')).toBeNull()
  })

  it('does not offer the approve tier Add a drug', async () => {
    role = 'admin'
    userId = 'ADM1'
    routeApi()
    renderView()
    await screen.findByText('Paracetamol 650mg Oral Tablet')
    expect(screen.queryByRole('button', { name: 'Add a drug' })).toBeNull()
  })

  it('marks a draft in the drug list as waiting for a second pharmacist', async () => {
    routeApi({}, [drug({ id: 'MED-NOVA', name: NEW_IDENTITY.name, identity_status: 'draft',
      identity_label: 'Waiting for a second pharmacist', orderable_identity: false })])
    renderView()
    expect(await screen.findByText('Waiting for a second pharmacist')).toBeInTheDocument()
  })

  it('lets the requester only withdraw, and another pharmacist confirm what was shown', async () => {
    routeApi({
      '/pharmacy-store/identity-requests/41/decision': () => ({
        request: { ...IDENTITY_REQUEST, decision: 'approved' },
        drug: drug({ id: 'MED-NOVA', ...NEW_IDENTITY }),
      }),
    })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Approvals' }))
    expect(await screen.findByRole('button', { name: 'Withdraw draft' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Confirm drug' })).toBeNull()
    expect(screen.getByText(/You asked for this, so another pharmacist must check it/))
      .toBeInTheDocument()
  })

  it('confirms with the identity shown, and refuses only with a reason', async () => {
    userId = 'PHA2'
    routeApi({
      '/pharmacy-store/identity-requests/41/decision': () => ({
        request: { ...IDENTITY_REQUEST, decision: 'approved' },
        drug: drug({ id: 'MED-NOVA', ...NEW_IDENTITY }),
      }),
    })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Approvals' }))
    expect(await screen.findByText('Screened as Amoxicillin.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Refuse drug' }))
    expect(await screen.findByText('Give the reason for refusing.')).toBeInTheDocument()
    expect(calls('/pharmacy-store/identity-requests/41/decision')).toHaveLength(0)

    fireEvent.click(screen.getByRole('button', { name: 'Confirm drug' }))
    await waitFor(() => expect(calls('/pharmacy-store/identity-requests/41/decision')).toHaveLength(1))
    expect(body(calls('/pharmacy-store/identity-requests/41/decision')[0]))
      .toEqual({ decision: 'approved', expected: NEW_IDENTITY })
    expect(await screen.findByText(/is confirmed and can now be ordered/)).toBeInTheDocument()
  })

  it('retires a drug only with a reason', async () => {
    routeApi({
      '/pharmacy-store/drugs/MED-PARA/retire': () => ({
        drug: drug({ identity_status: 'retired', identity_label: 'Retired' }) }),
    })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Retire this drug' }))
    const form = within(screen.getByRole('form', { name: 'Retire this drug' }))
    fireEvent.click(form.getByRole('button', { name: 'Retire' }))
    expect(await screen.findByText('Say why the drug is being retired.')).toBeInTheDocument()
    expect(calls('/pharmacy-store/drugs/MED-PARA/retire')).toHaveLength(0)
    fireEvent.change(form.getByLabelText('Why is it being retired?'),
      { target: { value: 'Generic name entered wrongly' } })
    fireEvent.click(form.getByRole('button', { name: 'Retire' }))
    await waitFor(() => expect(calls('/pharmacy-store/drugs/MED-PARA/retire')).toHaveLength(1))
    expect(body(calls('/pharmacy-store/drugs/MED-PARA/retire')[0]))
      .toEqual({ reason: 'Generic name entered wrongly' })
  })

  it('sends a correction for checking and offers the brand tick on a same-identity refusal', async () => {
    let attempt = 0
    routeApi({
      '/pharmacy-store/drugs/MED-PARA/identity-correction': () => {
        attempt += 1
        if (attempt === 1) {
          const words = 'the drug master already holds this generic name, strength and form: Dolo 650mg Tablet.'
          throw Object.assign(new Error(words), {
            status: 409, detail: { error: 'same_identity_exists', message: words } })
        }
        return { request: { ...IDENTITY_REQUEST, change_kind: 'identity_correction' }, drug: drug() }
      },
    })
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Open' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Correct the identity' }))
    const form = within(screen.getByRole('form', { name: 'Correct the identity' }))
    fireEvent.change(form.getByLabelText('Strength'), { target: { value: '650 mg' } })
    fireEvent.change(form.getByLabelText('Why does it need correcting?'),
      { target: { value: 'Strength spacing' } })
    fireEvent.click(form.getByRole('button', { name: 'Send for checking' }))
    expect(await screen.findByText(/already holds this generic name, strength and form/))
      .toBeInTheDocument()
    fireEvent.click(form.getByLabelText('This is another brand of the same medicine'))
    fireEvent.click(form.getByRole('button', { name: 'Send for checking' }))
    expect(await screen.findByText(/Sent to another pharmacist to check/)).toBeInTheDocument()
    const sent = calls('/pharmacy-store/drugs/MED-PARA/identity-correction')
    expect(body(sent[1])).toEqual({
      name: 'Paracetamol 650mg Oral Tablet', generic_name: 'Paracetamol', form: 'Tablet',
      strength: '650 mg', reason: 'Strength spacing', another_brand: true,
    })
  })
})
