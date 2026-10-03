/**
 * PayerPanel — the four things that must be true on this screen.
 *
 *  1. IT SAYS ORB SENDS NOTHING, on the first paint, before any network call,
 *     and still when every call fails. The failure this prevents is a clerk who
 *     believes Orb submitted the claim and therefore does not submit it.
 *  2. IT NEVER OFFERS A MOVE THE SERVER WOULD REFUSE. The action buttons are
 *     derived from the server's own transition table, so a denied claim shows
 *     no way to record a settlement — the shortcut that would let a desk clear
 *     its denial list by marking things paid.
 *  3. IT NEVER DRAWS AN UNKNOWN AMOUNT AS ₹0.00. "Not known" and "nothing" are
 *     opposite facts that look identical as a zero.
 *  4. THE SHORT-SETTLEMENT LEDGER RENDERS THE SERVER'S OWN FIGURES. A client
 *     that recomputed and rounded differently would draw a screen that
 *     disagrees with the record it just created, and the clerk believes the
 *     screen.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  getMeta: vi.fn(),
  getSummary: vi.fn(),
  getWorkQueue: vi.fn(),
  getClaim: vi.fn(),
  getClaims: vi.fn(),
  getDenials: vi.fn(),
  getPayers: vi.fn(),
  getPolicies: vi.fn(),
  recordSettlement: vi.fn(),
  recordSubmission: vi.fn(),
  createClaim: vi.fn(),
  savePayer: vi.fn(),
  getPatients: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  apiFetch: vi.fn(),
  API_BASE: '/api',
  buildHeaders: () => ({}),
  // The claim and cover forms pick the patient from the ward list.
  getPatients: api.getPatients,
}))

vi.mock('../../api/payer', async () => {
  const actual = await vi.importActual<typeof import('../../api/payer')>('../../api/payer')
  return {
    ...actual,
    getMeta: api.getMeta,
    getSummary: api.getSummary,
    getWorkQueue: api.getWorkQueue,
    getClaim: api.getClaim,
    getClaims: api.getClaims,
    getDenials: api.getDenials,
    getPayers: api.getPayers,
    getPolicies: api.getPolicies,
    recordSettlement: api.recordSettlement,
    recordSubmission: api.recordSubmission,
    createClaim: api.createClaim,
    savePayer: api.savePayer,
  }
})

import PayerPanel from './PayerPanel'
import type { Claim, ClaimStatus } from '../../api/payer'
import { formatClinicalDateTime } from '../../lib/datetime'

const SERVER_NOTICE =
  'Orb does not connect to any TPA, insurer or government scheme. '
  + 'Nothing on this screen is transmitted to a payer. '
  + 'Recording a pre-authorisation, a submission or a settlement here records '
  + "what the desk did elsewhere, on the payer's portal, by e-mail or on paper."

const SERVER_NOTICE_SHORT = 'Recorded in Orb only, not sent to the payer.'

const wrapped = { notice: SERVER_NOTICE, notice_short: SERVER_NOTICE_SHORT, transmits_to_payer: false as const }

/** Click, then let the click's own promises settle.
 *  `@testing-library/user-event` is not installed in this repo, and opening the
 *  drawer fires an async fetch — so the flush has to be explicit or the assertion
 *  races the state update React warns about. */
async function click(el: HTMLElement): Promise<void> {
  await act(async () => {
    fireEvent.click(el)
  })
}

function makeClaim(overrides: Partial<Claim> = {}): Claim {
  const status = (overrides.status ?? 'submitted') as ClaimStatus
  return {
    id: 1,
    claim_number: 'CLM-0001',
    patient_id: 'PAT-1',
    patient_name: 'Meera Rajan',
    admission_id: null,
    invoice_id: null,
    payer_id: 2,
    payer_code: 'STARTPA',
    payer_name: 'Star TPA',
    payer_type: 'tpa',
    claim_submission_mode: 'portal',
    portal_url: null,
    policy_id: 5,
    policy_number: 'POL-991',
    claim_type: 'cashless',
    package_code: null,
    preauth_number: null,
    preauth_requested_paise: null,
    preauth_approved_paise: null,
    preauth_status: null,
    claimed_paise: 10_000_000,
    claimed_display: '₹1,00,000.00',
    approved_paise: null,
    disallowed_paise: null,
    tds_paise: 0,
    settled_paise: null,
    patient_liability_paise: null,
    status,
    external_ref: null,
    submitted_at: '2026-07-20 09:00:00',
    decided_at: null,
    settled_at: null,
    created_at: '2026-07-19 09:00:00',
    notes: null,
    settlement: {
      claim_type: 'cashless',
      claimed_paise: 10_000_000,
      claimed_display: '₹1,00,000.00',
      approved_paise: null,
      approved_display: 'Awaiting decision',
      disallowed_paise: null,
      disallowed_display: 'Not yet known',
      copay_percent_bp: 0,
      copay_paise: null,
      copay_display: 'Not yet known',
      tds_paise: 0,
      tds_display: '₹0.00',
      expected_receipt_paise: null,
      expected_receipt_display: 'Not yet known',
      settled_paise: null,
      settled_display: 'Nothing received',
      short_settled_paise: null,
      short_settled_display: 'Not yet known',
      patient_liability_paise: null,
      patient_liability_display: 'Not yet known',
      hospital_receivable_paise: null,
      hospital_receivable_display: 'Not yet known',
      is_short_settled: false,
      is_over_settled: false,
      is_decided: false,
      is_received: false,
    },
    settlement_matches_record: true,
    stored_patient_liability_paise: null,
    age_days: 23,
    age_bucket: '15-30',
    aging_anchor: '2026-07-20 09:00:00',
    aging_anchor_source: 'event:submitted',
    aging_anchor_label: 'Submitted to payer',
    is_stale: true,
    is_open: true,
    legal_next_statuses: ['approved', 'cancelled', 'denied', 'partially_approved', 'queried'],
    hospital_receivable_applies: true,
    transmission_notice: SERVER_NOTICE_SHORT,
    events: [],
    ...overrides,
  }
}

const summary = {
  ...wrapped,
  open_claims: 3,
  stale_claims: 1,
  denied_open: 1,
  awaiting_preauth: 0,
  confirmed_receivable_paise: 0,
  confirmed_receivable_display: '₹0.00',
  estimated_receivable_paise: 10_000_000,
  estimated_receivable_display: '₹1,00,000.00',
  short_settled_count: 1,
  short_settled_paise: 500_000,
  short_settled_display: '₹5,000.00',
}

const meta = {
  ...wrapped,
  payer_types: ['tpa' as const],
  claim_types: ['cashless' as const],
  claim_statuses: ['draft' as const],
  legal_transitions: {},
  aging_buckets: ['0-7', '8-14', '15-30', '31-60', '60+'],
  denial_reason_codes: [{ code: 'EXCLUSION', label: 'Treatment excluded under the policy' }],
  denial_codes_are_local: true,
}

function queueWith(claims: Claim[]) {
  return {
    ...wrapped,
    generated_at: '2026-08-12 10:00:00',
    total: claims.length,
    buckets: [
      { bucket: '0-7', count: 0, claimed_paise: 0, claimed_display: '₹0.00' },
      { bucket: '15-30', count: claims.length, claimed_paise: 10_000_000, claimed_display: '₹1,00,000.00' },
    ],
    stale_after_days: 15,
    stale_count: claims.filter(c => c.is_stale).length,
    awaiting_payer: 1,
    awaiting_hospital: 0,
    claims,
  }
}

beforeEach(() => {
  api.getMeta.mockResolvedValue(meta)
  api.getSummary.mockResolvedValue(summary)
  api.getWorkQueue.mockResolvedValue(queueWith([makeClaim()]))
  api.getClaims.mockResolvedValue({ ...wrapped, claims: [], total: 0 })
  api.getDenials.mockResolvedValue({ ...wrapped, codes_are_local: 'local', total: 0, by_code: [], denials: [] })
  api.getPayers.mockResolvedValue({ ...wrapped, payers: [] })
  api.getPolicies.mockResolvedValue({ ...wrapped, policies: [] })
  api.getPatients.mockResolvedValue([
    { id: 'PAT-1', name: 'Meera Rajan', mrn: 'MRN-GM011', bed: 'GM-111', ward: 'GM-1' },
  ])
})

afterEach(() => {
  vi.clearAllMocks()
  // NOTE: no manual body wipe. The drawer is portalled to document.body, and
  // clearing innerHTML here rips the portal host out from under RTL's own
  // cleanup, which then throws "node to be removed is not a child of this node".
})

/* ── 1. The honesty rule ─────────────────────────────────────────────────── */

describe('the no-transmission notice', () => {
  it('is on screen before any request resolves', () => {
    // Every call left pending: this is the very first frame.
    api.getMeta.mockReturnValue(new Promise(() => {}))
    api.getSummary.mockReturnValue(new Promise(() => {}))
    api.getWorkQueue.mockReturnValue(new Promise(() => {}))

    render(<PayerPanel />)
    const note = screen.getByRole('note')
    expect(note).toHaveTextContent(/does not connect to any TPA/i)
    expect(note).toHaveTextContent(/Nothing on this screen is transmitted to a payer/i)
  })

  it('survives every request failing', async () => {
    api.getMeta.mockRejectedValue(new Error('offline'))
    api.getSummary.mockRejectedValue(new Error('offline'))
    api.getWorkQueue.mockRejectedValue(new Error('offline'))

    render(<PayerPanel />)
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
    expect(screen.getByRole('note')).toHaveTextContent(/does not connect to any TPA/i)
  })

  it('matches the server wording exactly, character for character', () => {
    // The panel keeps its own copy so the words paint before the network
    // answers. Asserted against what is RENDERED, not against an exported
    // constant: if the server's sentence changes and the panel's does not, the
    // two disagree, and this is where that is caught.
    api.getMeta.mockReturnValue(new Promise(() => {}))
    api.getSummary.mockReturnValue(new Promise(() => {}))
    api.getWorkQueue.mockReturnValue(new Promise(() => {}))

    render(<PayerPanel />)
    expect(screen.getByRole('note').textContent).toBe(SERVER_NOTICE)
  })

  it('repeats on the claim drawer, next to the buttons that record actions', async () => {
    const claim = makeClaim()
    api.getClaim.mockResolvedValue({ ...wrapped, claim })
    render(<PayerPanel />)
    await click(await screen.findByRole('button', { name: /Meera Rajan/ }))
    const drawer = await screen.findByRole('dialog')
    expect(within(drawer).getAllByText(/not sent to the payer/i).length).toBeGreaterThan(0)
  })
})

/* ── 2. The lifecycle is not negotiable from the client ──────────────────── */

describe('a denied claim offers no way to record money', () => {
  it('shows an appeal, and no settlement action', async () => {
    const denied = makeClaim({
      status: 'denied',
      legal_next_statuses: ['appealed', 'closed'],
      approved_paise: 0,
      settlement: {
        ...makeClaim().settlement,
        approved_paise: 0,
        approved_display: '₹0.00',
        disallowed_paise: 10_000_000,
        disallowed_display: '₹1,00,000.00',
        is_decided: true,
      },
    })
    api.getWorkQueue.mockResolvedValue(queueWith([denied]))
    api.getClaim.mockResolvedValue({ ...wrapped, claim: denied })

    render(<PayerPanel />)
    await click(await screen.findByRole('button', { name: /Meera Rajan/ }))
    const drawer = await screen.findByRole('dialog')

    expect(within(drawer).queryByRole('button', { name: /Record money received/i })).toBeNull()
    expect(within(drawer).getByRole('button', { name: /Record an appeal/i })).toBeInTheDocument()
    // And the reason it matters is spelled out where the clerk will read it.
    expect(within(drawer).getByText(/only reach a settlement through an\s+appeal/i)).toBeInTheDocument()
  })

  it('offers the settlement action once the claim is approved', async () => {
    const approved = makeClaim({
      status: 'approved',
      legal_next_statuses: ['settled', 'closed'],
    })
    api.getWorkQueue.mockResolvedValue(queueWith([approved]))
    api.getClaim.mockResolvedValue({ ...wrapped, claim: approved })

    render(<PayerPanel />)
    await click(await screen.findByRole('button', { name: /Meera Rajan/ }))
    const drawer = await screen.findByRole('dialog')
    expect(within(drawer).getByRole('button', { name: /Record money received/i })).toBeInTheDocument()
  })
})

/* ── 3. Not configured is not zero ───────────────────────────────────────── */

describe('unknown amounts', () => {
  it('render as words, never as a zero', async () => {
    api.getClaim.mockResolvedValue({ ...wrapped, claim: makeClaim() })
    render(<PayerPanel />)
    await click(await screen.findByRole('button', { name: /Meera Rajan/ }))
    const drawer = await screen.findByRole('dialog')

    expect(within(drawer).getByText('Awaiting decision')).toBeInTheDocument()
    expect(within(drawer).getAllByText('Not yet known').length).toBeGreaterThan(0)
    expect(within(drawer).getByText('Nothing received')).toBeInTheDocument()
    // The undecided claim shows exactly one ₹0.00 — the TDS, which genuinely is
    // zero. No unknown amount has been drawn as a zero.
    expect(within(drawer).getAllByText('₹0.00')).toHaveLength(1)
  })
})

/* ── 4. The ledger is the server's arithmetic, not the client's ──────────── */

describe('the short-settlement ledger', () => {
  it('renders the server figures, to the paise', async () => {
    const settled = makeClaim({
      status: 'settled',
      legal_next_statuses: ['closed'],
      settlement: {
        claim_type: 'cashless',
        claimed_paise: 10_000_000,
        claimed_display: '₹1,00,000.00',
        approved_paise: 8_000_000,
        approved_display: '₹80,000.00',
        disallowed_paise: 2_000_000,
        disallowed_display: '₹20,000.00',
        copay_percent_bp: 1000,
        copay_paise: 800_000,
        copay_display: '₹8,000.00',
        tds_paise: 80_000,
        tds_display: '₹800.00',
        expected_receipt_paise: 7_120_000,
        expected_receipt_display: '₹71,200.00',
        settled_paise: 6_500_000,
        settled_display: '₹65,000.00',
        short_settled_paise: 620_000,
        short_settled_display: '₹6,200.00',
        patient_liability_paise: 2_800_000,
        patient_liability_display: '₹28,000.00',
        hospital_receivable_paise: 7_120_000,
        hospital_receivable_display: '₹71,200.00',
        is_short_settled: true,
        is_over_settled: false,
        is_decided: true,
        is_received: true,
      },
    })
    api.getWorkQueue.mockResolvedValue(queueWith([settled]))
    api.getClaim.mockResolvedValue({ ...wrapped, claim: settled })

    render(<PayerPanel />)
    await click(await screen.findByRole('button', { name: /Meera Rajan/ }))
    const drawer = await screen.findByRole('dialog')

    for (const amount of ['₹1,00,000.00', '₹80,000.00', '₹20,000.00', '₹8,000.00',
      '₹800.00', '₹71,200.00', '₹65,000.00', '₹6,200.00', '₹28,000.00']) {
      expect(within(drawer).getAllByText(amount).length).toBeGreaterThan(0)
    }
    expect(within(drawer).getByText('Short-settled by')).toBeInTheDocument()
    // Indian grouping, not ₹100,000.00 — the figure a clerk checks by eye.
    expect(within(drawer).queryByText('₹100,000.00')).toBeNull()
  })

  it('names the over-settlement case rather than showing a negative shortfall', async () => {
    const over = makeClaim({
      status: 'settled',
      legal_next_statuses: ['closed'],
      settlement: {
        ...makeClaim().settlement,
        approved_paise: 1_000_000,
        approved_display: '₹10,000.00',
        disallowed_paise: 0,
        disallowed_display: '₹0.00',
        copay_paise: 0,
        copay_display: '₹0.00',
        expected_receipt_paise: 1_000_000,
        expected_receipt_display: '₹10,000.00',
        settled_paise: 1_100_000,
        settled_display: '₹11,000.00',
        short_settled_paise: -100_000,
        short_settled_display: '-₹1,000.00',
        patient_liability_paise: 0,
        patient_liability_display: '₹0.00',
        is_decided: true,
        is_received: true,
        is_over_settled: true,
      },
    })
    api.getWorkQueue.mockResolvedValue(queueWith([over]))
    api.getClaim.mockResolvedValue({ ...wrapped, claim: over })

    render(<PayerPanel />)
    await click(await screen.findByRole('button', { name: /Meera Rajan/ }))
    const drawer = await screen.findByRole('dialog')
    expect(within(drawer).getByText('Over-settled by')).toBeInTheDocument()
    expect(within(drawer).queryByText('Short-settled by')).toBeNull()
  })
})

/* ── The aging view ──────────────────────────────────────────────────────── */

describe('the work queue', () => {
  it('names what the age is counted from, not just a number of days', async () => {
    render(<PayerPanel />)
    const row = await screen.findByRole('button', { name: /Meera Rajan/ })
    expect(within(row).getByText('23')).toBeInTheDocument()
    expect(within(row).getByText(/since submitted to payer/i)).toBeInTheDocument()
  })

  it('says whose move it is', async () => {
    render(<PayerPanel />)
    const row = await screen.findByRole('button', { name: /Meera Rajan/ })
    expect(within(row).getByText('Payer to act')).toBeInTheDocument()
  })

  it('filters by aging bucket', async () => {
    const fresh = makeClaim({ id: 2, patient_name: 'Arun Das', age_days: 2, age_bucket: '0-7', is_stale: false })
    api.getWorkQueue.mockResolvedValue(queueWith([makeClaim(), fresh]))
    render(<PayerPanel />)
    await screen.findByRole('button', { name: /Meera Rajan/ })
    expect(screen.getByRole('button', { name: /Arun Das/ })).toBeInTheDocument()

    await click(screen.getByRole('button', { name: /15-30 days/ }))
    expect(screen.getByRole('button', { name: /Meera Rajan/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Arun Das/ })).toBeNull()
  })
})

/* ── The panel never claims to transmit ──────────────────────────────────── */

describe('no button on this screen offers to send anything', () => {
  it('labels the submission action as a record of what the desk did', async () => {
    const draft = makeClaim({ status: 'draft', legal_next_statuses: ['preauth_requested', 'submitted', 'cancelled'] })
    api.getWorkQueue.mockResolvedValue(queueWith([draft]))
    api.getClaim.mockResolvedValue({ ...wrapped, claim: draft })

    render(<PayerPanel />)
    await click(await screen.findByRole('button', { name: /Meera Rajan/ }))
    const drawer = await screen.findByRole('dialog')

    expect(within(drawer).getByRole('button', { name: /Record that we submitted this/i })).toBeInTheDocument()
    expect(within(drawer).getByText(/Submit the claim on the payer's portal first/i)).toBeInTheDocument()

    for (const lie of [/^Submit claim$/i, /^Send to payer$/i, /^Transmit/i]) {
      expect(within(drawer).queryByRole('button', { name: lie })).toBeNull()
    }
  })
})

/* ── F095: each action form owns its fields; a claim opens once ──────────── */

describe('the claim drawer forms do not share fields', () => {
  it('typing a note leaves the denial reason empty and the denial button off', async () => {
    const submitted = makeClaim()  // submitted: decision, query and note forms all show
    api.getClaim.mockResolvedValue({ ...wrapped, claim: submitted })
    render(<PayerPanel />)
    await click(await screen.findByRole('button', { name: /Meera Rajan/ }))
    const drawer = await screen.findByRole('dialog')

    fireEvent.change(within(drawer).getByLabelText('Note for the file'),
      { target: { value: 'Called the TPA, said Tuesday' } })

    expect(within(drawer).getByLabelText(/payer's reason, in the payer's own words/i)).toHaveValue('')
    expect(within(drawer).getByLabelText('What the payer asked for')).toHaveValue('')
    expect(within(drawer).getByRole('button', { name: 'Record a denial' })).toBeDisabled()
    expect(within(drawer).getByRole('button', { name: 'Record a payer query' })).toBeDisabled()
    expect(within(drawer).getByRole('button', { name: 'Add note' })).toBeEnabled()
  })
})

describe('opening a claim file', () => {
  it('a double click sends one request, carrying an idempotency key', async () => {
    let release: (v: unknown) => void = () => {}
    api.createClaim.mockReturnValue(new Promise(r => { release = r }))
    render(<PayerPanel />)
    await click(await screen.findByRole('tab', { name: 'All claims' }))
    await pickPatient('Meera Rajan')
    const open = screen.getByRole('button', { name: 'Open claim' })
    fireEvent.click(open)
    fireEvent.click(open)
    expect(api.createClaim).toHaveBeenCalledTimes(1)
    expect(api.createClaim.mock.calls[0][0].idempotency_key).toBeTruthy()
    expect(open).toBeDisabled()
    await act(async () => { release({ ...wrapped, claim: makeClaim({ status: 'draft' }) }) })
    expect(open).toBeEnabled()
  })
})

/* ── F093: money screens name the patient, they do not ask for an id ─────── */

/** Search the ward list by name and pick the patient, as the desk does. */
async function pickPatient(name: string): Promise<void> {
  const box = await screen.findByRole('combobox', { name: /^patient$/i })
  await waitFor(() => expect(box).toBeEnabled())
  await act(async () => { fireEvent.focus(box) })
  await act(async () => { fireEvent.change(box, { target: { value: name.slice(0, 4) } }) })
  await click(await screen.findByRole('option', { name: new RegExp(name) }))
}

describe('identifying the patient', () => {
  it('opens a claim for a patient picked by name, not a typed id', async () => {
    api.createClaim.mockResolvedValue({ ...wrapped, claim: makeClaim({ status: 'draft' }) })
    render(<PayerPanel />)
    await click(await screen.findByRole('tab', { name: 'All claims' }))
    expect(screen.queryByLabelText('Patient ID')).toBeNull()
    await pickPatient('Meera Rajan')
    await click(screen.getByRole('button', { name: 'Open claim' }))
    await waitFor(() => expect(api.createClaim).toHaveBeenCalled())
    expect(api.createClaim.mock.calls[0][0].patient_id).toBe('PAT-1')
  })

  it('refuses to open a claim before a patient is chosen', async () => {
    render(<PayerPanel />)
    await click(await screen.findByRole('tab', { name: 'All claims' }))
    await click(await screen.findByRole('button', { name: 'Open claim' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/choose the patient/i)
    expect(api.createClaim).not.toHaveBeenCalled()
  })

  it('lists cover by the patient name and MRN, not the internal id', async () => {
    api.getPolicies.mockResolvedValue({ ...wrapped, policies: [{
      id: 3, patient_id: 'PAT-50A2D09E', patient_name: 'Meera Rajan', patient_mrn: 'MRN-GM011',
      admission_id: null, payer_id: 2, payer_code: 'STARTPA', payer_name: 'Star TPA',
      payer_type: 'tpa', policy_number: 'POL-991', member_id: null,
      scheme_beneficiary_id: null, subscriber_name: null, valid_from: null, valid_to: null,
      sum_insured_paise: null, sum_insured_is_configured: false, sum_insured_display: 'Not known',
      copay_percent_bp: 0, copay_display: 'None', preauth_required: false, is_primary: true,
      status: 'active', verified_by: null, verified_at: null, claimable: true,
    }] })
    render(<PayerPanel />)
    await click(await screen.findByRole('tab', { name: 'Patient cover' }))
    const head = (await screen.findByText('Meera Rajan')).closest('.payer-policy__head')!
    expect(head).toHaveTextContent('MRN-GM011')
    expect(head).toHaveTextContent('Star TPA')
    expect(head).not.toHaveTextContent('PAT-50A2D09E')
  })
})

/* ── F097: an update starts from what is on file ─────────────────────────── */

describe('updating a payer', () => {
  it('loads the saved payer, says its tariff link is kept, and never sends one', async () => {
    const star = {
      id: 9, code: 'STARTPA', name: 'Star TPA', payer_type: 'tpa',
      tariff_plan_id: 4, tariff_plan_code: 'STAR-2026', claim_submission_mode: 'email',
      portal_url: null, credit_days: 45, credit_days_display: '45 days',
      preauth_required: true, is_active: true, claimable: true,
      contact_name: null, contact_phone: null, contact_email: null, notes: null,
    }
    api.getPayers.mockResolvedValue({ ...wrapped, payers: [star] })
    api.savePayer.mockResolvedValue({ ...wrapped, payer: { ...star, name: 'Star Health TPA' } })
    render(<PayerPanel />)
    await click(await screen.findByRole('tab', { name: 'Payers' }))
    await screen.findByText('Star TPA')

    fireEvent.change(screen.getByLabelText('Code'), { target: { value: 'startpa' } })
    expect(screen.getByLabelText('Name')).toHaveValue('Star TPA')
    expect(screen.getByLabelText('How the desk submits (outside Orb)')).toHaveValue('email')
    expect(screen.getByText(/tariff plan\s*\(STAR-2026\)/)).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Star Health TPA' } })
    await click(screen.getByRole('button', { name: 'Save payer' }))
    const body = api.savePayer.mock.calls[0][0]
    expect(body).toMatchObject({ code: 'STARTPA', name: 'Star Health TPA', claim_submission_mode: 'email' })
    expect('tariff_plan_id' in body).toBe(false)
  })
})

/* ── F024: history times are UTC on the server ───────────────────────────── */

describe('the claim history', () => {
  it('renders a bare UTC timestamp in local time, not the UTC clock', async () => {
    // payer_service stamps events "YYYY-MM-DD HH:MM:SS" in UTC. Slicing that
    // string printed 13:21 for an event the desk recorded at 06:51 PM IST.
    const claim = makeClaim({
      events: [{
        id: 1, claim_id: 1, event_type: 'submitted', from_status: 'draft',
        to_status: 'submitted', amount_paise: null, amount_display: '',
        denial_code: null, denial_reason: null, remark: null, document_ref: null,
        actor_id: 'ADM001', occurred_at: '2026-08-10 13:21:00',
        recorded_at: '2026-08-10 13:21:00',
      }],
    })
    api.getClaim.mockResolvedValue({ ...wrapped, claim })
    render(<PayerPanel />)
    await click(await screen.findByRole('button', { name: /Meera Rajan/ }))
    const drawer = await screen.findByRole('dialog')
    const when = drawer.querySelector('.payer-timeline__when')!
    expect(when.textContent).toBe(formatClinicalDateTime(new Date(Date.UTC(2026, 7, 10, 13, 21))))
    expect(when.textContent).not.toContain('2026-08-10 13:21')
  })
})
