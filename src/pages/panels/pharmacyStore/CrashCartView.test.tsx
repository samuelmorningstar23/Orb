/**
 * Pharmacy store, Crash carts (grn-23): what the pharmacy's screen must get
 * right.
 *
 *  1. The view registers as 'crash-carts', order 110, behind pharmacy_store.read.
 *  2. With ward stock switched off it says so and asks the server nothing.
 *  3. Each cart shows its seal, its due checks with the reasons, and expired,
 *     expiring and held batches with what to do.
 *  4. The top-up after use lists exactly the server's suggestion and asks for
 *     it (urgent by default) with one request.
 *  5. A full check counts every batch, needs a new seal, needs a note when the
 *     count differs, and is sent with a request key.
 *  6. A check waiting for the pharmacy is confirmed, or refused with a reason,
 *     and never by the person who counted.
 *  7. The batch trace lists the carts a batch went to.
 *
 * Driven through a mocked apiFetch, so the real src/api/pharmacyStore files
 * build the URLs and bodies.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import CrashCartView, { viewId, viewLabel, viewOrder, viewPermission } from './CrashCartView'

const apiFetch = vi.fn()
let userId = 'PHA2'

vi.mock('../../../api/client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
  errorDetailMessage: (_d: unknown, status: number) => `Request failed (${status})`,
  handleResponseError: async () => { throw new Error('x') },
  getUser: () => ({ id: userId, name: 'Test user', role: 'pharmacist' }),
}))

const ON = { features: { pharmacy_ward_stock: true } }

function pending(overrides: Record<string, unknown> = {}) {
  return {
    id: 41, store_id: 9, store_code: 'CC-ICU', store_name: 'ICU crash cart',
    department: 'Critical Care', kind: 'full', kind_label: 'Full check', status: 'submitted',
    status_label: 'Waiting for the pharmacy', seal_found: null, seal_intact: false,
    seal_expected: 'S-100', seal_matches: null, seal_applied: 'S-101',
    note: 'Two used at a cardiac arrest', counted_by: 'NUR1', counted_by_name: 'Priya Reddy',
    checked_at: '2026-10-10 08:00:00', decided_by: null, decided_by_name: null,
    decided_at: null, decision_reason: null, short_units: 2, over_units: 0,
    outcome: 'No seal on the cart; count 2 units short; sealed with S-101.', ...overrides,
  }
}

function cart(overrides: Record<string, unknown> = {}) {
  return {
    store: { id: 9, code: 'CC-ICU', name: 'ICU crash cart', department: 'Critical Care',
      ward: 'ICU-1', is_active: true, pharmacy_kind: 'crash_cart', kind_label: 'Crash cart' },
    drugs: [{
      drug_id: 'MED-ADR', drug_name: 'Adrenaline 1 mg/ml injection', form: 'Injection',
      strength: '1 mg/ml', dispense_unit: 'ampoule', listed: true, par_units: 5,
      max_units: null, on_hand_units: 5, held_units: 0, expired_units: 0,
      in_transit_units: 0, requested_units: 0, earliest_expiry: '2027-01-31',
      below_par: true, top_up_units: 2, found_short_units: 2, problem: null,
      lots: [
        { lot_id: 'L1', batch_no: 'ADR-1', expiry_date: '2027-01-31', units: 4,
          status: 'available', status_label: 'available' },
        { lot_id: 'L2', batch_no: 'ADR-0', expiry_date: '2026-10-20', units: 1,
          status: 'available', status_label: 'available' },
      ],
    }],
    seal: 'S-100', seal_state: 'sealed',
    last_check: null, last_full_check: null, pending_check: null,
    seal_check_due: true, full_check_due: true,
    due_reasons: ['No full check is on record.'],
    expired: [],
    expiring: [{ drug_id: 'MED-ADR', drug_name: 'Adrenaline 1 mg/ml injection', lot_id: 'L2',
      batch_no: 'ADR-0', expiry_date: '2026-10-20', units: 1, status: 'available',
      status_label: 'available', days_left: 10 }],
    held: [],
    top_up: [{ drug_id: 'MED-ADR', drug_name: 'Adrenaline 1 mg/ml injection', units: 2,
      dispense_unit: 'ampoule' }],
    below_par: 1, not_allowed: [],
    ...overrides,
  }
}

let overview = {
  today: '2026-10-10', near_expiry_days: 90, seal_check_due_hours: 24,
  full_check_due_days: 30, carts: [cart()], may_check: true, may_confirm: true,
  may_raise: true, priorities: ['routine', 'urgent', 'emergency'],
}

function routeApi() {
  apiFetch.mockImplementation(async (path: string, init?: RequestInit) => {
    const post = init?.method === 'POST'
    if (path.startsWith('/pharmacy-store/ward/crash-carts/checks/41/decision') && post) {
      return { check: pending({ status: 'approved' }) }
    }
    if (path.startsWith('/pharmacy-store/ward/crash-carts/checks/41')) {
      return { check: pending({ lines: [{
        id: 1, line_no: 1, lot_id: 'L1', drug_id: 'MED-ADR',
        drug_name: 'Adrenaline 1 mg/ml injection', batch_no: 'ADR-1',
        expiry_date: '2027-01-31', system_units: 4, counted_units: 2, short_units: 2,
        over_units: 0, found_extra: false, expired_at_check: false,
        adjustment_movement_ref: null }] }) }
    }
    if (path.startsWith('/pharmacy-store/ward/crash-carts/checks')) {
      return { checks: [pending()] }
    }
    if (path.startsWith('/pharmacy-store/ward/crash-carts/batch')) {
      return { drug_id: 'MED-ADR', drug_name: 'Adrenaline 1 mg/ml injection',
        batch_no: 'ADR-1', totals: {}, carts: [{
          store_id: 9, store_code: 'CC-ICU', store_name: 'ICU crash cart',
          department: 'Critical Care', transfers: [5], sent_units: 4, received_units: 4,
          in_transit_units: 0, in_cart_units: 1, unattributed_units: 3,
          label: 'Issued to ICU crash cart stock, patients not recorded' }] }
    }
    if (path.startsWith('/pharmacy-store/ward/crash-carts/CC-ICU/checks') && post) {
      return { check: pending({ status: 'approved', kind: 'full', outcome: 'Recorded.' }),
        replayed: false }
    }
    if (path.startsWith('/pharmacy-store/ward/crash-carts/CC-ICU/top-up') && post) {
      return { indent: { id: 3 }, left_out: [] }
    }
    if (path.startsWith('/pharmacy-store/ward/crash-carts')) return overview
    throw new Error(`unexpected ${path}`)
  })
}

function posts(prefix: string) {
  return apiFetch.mock.calls.filter(([path, init]) =>
    String(path).startsWith(prefix) && (init as RequestInit | undefined)?.method === 'POST')
}

function body(call: unknown[]) {
  return JSON.parse(String((call[1] as RequestInit).body))
}

function renderView(meta: Record<string, unknown> = ON) {
  const onModuleOff = vi.fn()
  render(<CrashCartView meta={meta} openView={() => {}} onModuleOff={onModuleOff} />)
  return { onModuleOff }
}

beforeEach(() => {
  userId = 'PHA2'
  overview = { ...overview, carts: [cart()], may_check: true, may_confirm: true, may_raise: true }
  apiFetch.mockReset()
  routeApi()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('CrashCartView', () => {
  it('registers as the crash cart view behind the read tier', () => {
    expect(viewId).toBe('crash-carts')
    expect(viewLabel).toBe('Crash carts')
    expect(viewOrder).toBe(110)
    expect(viewPermission).toBe('pharmacy_store.read')
  })

  it('says ward stock is switched off and asks the server nothing', () => {
    renderView({ features: { pharmacy_ward_stock: false } })
    expect(screen.getByText(/Ward stock is switched off on this site/)).toBeInTheDocument()
    expect(apiFetch).not.toHaveBeenCalled()
  })

  it('shows the seal, the due checks with their reasons and the expiring batch', async () => {
    renderView()
    expect(await screen.findByText('Sealed with S-100')).toBeInTheDocument()
    expect(screen.getByText('Seal check due')).toBeInTheDocument()
    expect(screen.getByText('Full check due')).toBeInTheDocument()
    expect(screen.getByText('No full check is on record.')).toBeInTheDocument()
    expect(screen.getByText('Expires in 10 days')).toBeInTheDocument()
    expect(screen.getByText('20 Oct 2026')).toBeInTheDocument()
  })

  it('asks for exactly the suggested top-up, urgent by default', async () => {
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Ask for the top-up' }))
    await screen.findByText(/Top-up asked for ICU crash cart/)
    const sent = posts('/pharmacy-store/ward/crash-carts/CC-ICU/top-up')
    expect(sent).toHaveLength(1)
    expect(body(sent[0])).toEqual({ priority: 'urgent' })
  })

  it('does not offer the top-up to a role the server says cannot ask', async () => {
    overview = { ...overview, may_raise: false }
    renderView()
    await screen.findByText('Sealed with S-100')
    expect(screen.queryByRole('button', { name: 'Ask for the top-up' })).toBeNull()
  })

  it('a full check counts every batch, seals the cart and needs a note when it differs',
    async () => {
      renderView()
      fireEvent.click(await screen.findByRole('button', { name: 'Record a check' }))
      fireEvent.click(screen.getByLabelText('Full check (count and seal again)'))
      fireEvent.change(screen.getByLabelText('Seal number found'), { target: { value: 'S-100' } })
      const record = screen.getByRole('button', { name: 'Record the check' })
      expect(record).toBeDisabled()
      fireEvent.change(screen.getByLabelText('Units of Adrenaline 1 mg/ml injection batch ADR-1 counted'),
        { target: { value: '2' } })
      fireEvent.change(screen.getByLabelText('Units of Adrenaline 1 mg/ml injection batch ADR-0 counted'),
        { target: { value: '1' } })
      fireEvent.change(screen.getByLabelText('New seal number'), { target: { value: 'S-101' } })
      // The count differs from the books: a note is needed first.
      expect(record).toBeDisabled()
      fireEvent.change(screen.getByLabelText('What happened (needed when the count differs)'),
        { target: { value: 'Two used at a cardiac arrest' } })
      expect(record).toBeEnabled()
      fireEvent.click(record)
      await screen.findByText(/Full check recorded/)
      const sent = posts('/pharmacy-store/ward/crash-carts/CC-ICU/checks')
      expect(sent).toHaveLength(1)
      const sentBody = body(sent[0])
      expect(sentBody).toMatchObject({
        kind: 'full', seal_found: 'S-100', seal_intact: true, seal_applied: 'S-101',
        lines: [{ lot_id: 'L1', counted_units: 2 }, { lot_id: 'L2', counted_units: 1 }],
        note: 'Two used at a cardiac arrest',
      })
      expect(sentBody.idempotency_key).toMatch(/^cart-check-/)
    })

  it('a seal check sends no count and no new seal', async () => {
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Record a check' }))
    fireEvent.click(screen.getByLabelText('Seal broken'))
    fireEvent.change(screen.getByLabelText('Seal number found'), { target: { value: 'S-100' } })
    fireEvent.click(screen.getByRole('button', { name: 'Record the check' }))
    await waitFor(() => expect(posts('/pharmacy-store/ward/crash-carts/CC-ICU/checks'))
      .toHaveLength(1))
    const sentBody = body(posts('/pharmacy-store/ward/crash-carts/CC-ICU/checks')[0])
    expect(sentBody).toMatchObject({ kind: 'seal', seal_found: 'S-100', seal_intact: false })
    expect(sentBody.lines).toBeUndefined()
    expect(sentBody.seal_applied).toBeUndefined()
  })

  it('confirms a check waiting for the pharmacy', async () => {
    overview = { ...overview, carts: [cart({ pending_check: pending() })] }
    renderView()
    expect(await screen.findByText('2 units short')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm the check' }))
    await screen.findByText(/Check confirmed/)
    expect(body(posts('/pharmacy-store/ward/crash-carts/checks/41/decision')[0]))
      .toEqual({ confirm: true, reason: null })
  })

  it('refusing a check needs a reason', async () => {
    overview = { ...overview, carts: [cart({ pending_check: pending() })] }
    renderView()
    const refuse = await screen.findByRole('button', { name: 'Refuse and count again' })
    expect(refuse).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Reason (needed to refuse)'),
      { target: { value: 'Count with the in-charge' } })
    fireEvent.click(refuse)
    await screen.findByText(/Check refused/)
    expect(body(posts('/pharmacy-store/ward/crash-carts/checks/41/decision')[0]))
      .toEqual({ confirm: false, reason: 'Count with the in-charge' })
  })

  it('never offers the decision to the person who counted', async () => {
    userId = 'NUR1'
    overview = { ...overview, carts: [cart({ pending_check: pending() })] }
    renderView()
    await screen.findByText(/someone else in the pharmacy confirms it/)
    expect(screen.queryByRole('button', { name: 'Confirm the check' })).toBeNull()
  })

  it('lists the carts a batch went to', async () => {
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Batch in carts' }))
    fireEvent.change(screen.getByLabelText('Drug code'), { target: { value: 'MED-ADR' } })
    fireEvent.change(screen.getByLabelText('Batch'), { target: { value: 'ADR-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Find the carts' }))
    expect(await screen.findByText('Issued to ICU crash cart stock, patients not recorded'))
      .toBeInTheDocument()
    const call = apiFetch.mock.calls.find(([path]) =>
      String(path).startsWith('/pharmacy-store/ward/crash-carts/batch'))
    expect(String(call?.[0])).toContain('drug_id=MED-ADR')
    expect(String(call?.[0])).toContain('batch_no=ADR-1')
  })

  it('shows the check record', async () => {
    renderView()
    fireEvent.click(await screen.findByRole('button', { name: 'Check record' }))
    expect(await screen.findByText('No seal on the cart; count 2 units short; sealed with S-101.'))
      .toBeInTheDocument()
  })
})
