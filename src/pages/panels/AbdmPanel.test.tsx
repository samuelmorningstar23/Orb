/**
 * AbdmPanel — the four things that must be true on this screen.
 *
 *  1. IT SAYS ORB IS NOT CONNECTED TO ABDM, on the first paint, before any
 *     network call, and still when every call fails. The failure this prevents
 *     is a clerk who believes Orb linked the care context and therefore does not
 *     link it — after which the patient's discharge summary never reaches their
 *     health account and nobody finds out for months.
 *  2. AN UNVERIFIED ABHA NEVER LOOKS VERIFIED. The words "Not verified" ride
 *     beside the number everywhere it appears, and no action that requires a
 *     verified ABHA is offered on an unverified one.
 *  3. AN UNLINK IS NEVER OFFERED WITHOUT A REASON, and an unlinked row still
 *     shows that it was once linked. An unlink that looked like a deletion would
 *     make "was this episode ever linked?" unanswerable.
 *  4. A REVOKED CONSENT OFFERS NO WAY BACK. There is no reinstate control, and
 *     the screen says in words that a fresh consent is a new artefact.
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const api = vi.hoisted(() => ({
  getMeta: vi.fn(),
  getSummary: vi.fn(),
  getQueue: vi.fn(),
  getIdentities: vi.fn(),
  getCareContexts: vi.fn(),
  getConsents: vi.fn(),
  unlinkCareContext: vi.fn(),
  revokeConsent: vi.fn(),
  checkAbha: vi.fn(),
  recordAbha: vi.fn(),
  recordVerification: vi.fn(),
  linkCareContext: vi.fn(),
}))

vi.mock('../../api/client', () => ({
  apiFetch: vi.fn(),
  API_BASE: '/api',
  buildHeaders: () => ({}),
}))

vi.mock('../../api/abdm', async () => {
  const actual = await vi.importActual<typeof import('../../api/abdm')>('../../api/abdm')
  return { ...actual, ...api }
})

import AbdmPanel from './AbdmPanel'
import { formatClinicalDateTime } from '../../lib/datetime'
import type {
  AbhaIdentity, CareContext, ConsentArtefact, QueueEntry,
} from '../../api/abdm'

const SERVER_NOTICE =
  'Orb is not connected to the ABDM gateway. '
  + 'Nothing on this screen is transmitted to ABDM, to a health information '
  + 'exchange or to a consent manager. '
  + 'Recording an ABHA, a verification, a care-context link or a consent '
  + "artefact here records what a person did elsewhere — on the ABDM portal, in "
  + "the ABHA app, or on another system's HIP console."

const SERVER_NOTICE_SHORT = 'Recorded in Orb only — nothing was sent to ABDM.'

const wrapped = {
  notice: SERVER_NOTICE,
  notice_short: SERVER_NOTICE_SHORT,
  gateway_connected: false as const,
  transmitted_to_abdm: false as const,
}

/** Click, then let the click's own promises settle. `user-event` is not
 *  installed in this repo, and these clicks fire async fetches. */
async function click(el: HTMLElement): Promise<void> {
  await act(async () => { fireEvent.click(el) })
}

/** Wait for the RENDER, not for the fetch call.
 *
 *  `waitFor(() => expect(api.getQueue).toHaveBeenCalled())` returns the moment
 *  the request is issued, which is one microtask before the rows exist — a race
 *  that failed roughly one run in six. Every interaction below waits on the
 *  element it is about to touch instead. */
const findButton = (name: string | RegExp) => screen.findByRole('button', { name })

function makeIdentity(overrides: Partial<AbhaIdentity> = {}): AbhaIdentity {
  const status = overrides.verification_status ?? 'unverified'
  return {
    id: 1,
    patient_id: 'PAT-1',
    patient_name: 'Meera Rajan',
    patient_mrn: 'MRN-1',
    patient_ward: 'W1',
    abha_number: '12345678901234',
    abha_number_formatted: '12-3456-7890-1234',
    abha_address: 'meera.rajan@abdm',
    verification_status: status,
    verification_label: status === 'verified' ? 'Verified' : 'Not verified',
    is_verified: status === 'verified',
    verified_at: null,
    verified_by: null,
    verification_method: null,
    verification_reference: null,
    is_primary: true,
    status: 'active',
    source: 'counter_keyed',
    notes: null,
    recorded_by: 'ADM-1',
    created_at: '2026-08-10 09:00:00',
    updated_at: '2026-08-10 09:00:00',
    transmitted_to_abdm: false,
    ...overrides,
  }
}

function makeQueueEntry(overrides: Partial<QueueEntry> = {}): QueueEntry {
  return {
    encounter_ref: 'ADM-1',
    patient_id: 'PAT-1',
    patient_name: 'Meera Rajan',
    patient_mrn: 'MRN-1',
    department: 'Medicine',
    ward: 'W1',
    encounter_status: 'active',
    encounter_at: '2026-08-05 08:00:00',
    admission_type: 'emergency',
    abha_identity_id: 1,
    abha_number: '12345678901234',
    abha_number_formatted: '12-3456-7890-1234',
    abha_address: 'meera.rajan@abdm',
    verification_status: 'unverified',
    verification_label: 'Not verified',
    bucket: 'abha_unverified',
    bucket_label: 'ABHA recorded but not verified',
    age_days: 7,
    is_stale: true,
    ...overrides,
  }
}

function makeCareContext(overrides: Partial<CareContext> = {}): CareContext {
  return {
    id: 11,
    abha_identity_id: 1,
    patient_id: 'PAT-1',
    patient_name: 'Meera Rajan',
    care_context_reference: 'ADM-1',
    display: 'Admission 5 Aug',
    hi_type: 'DischargeSummary',
    encounter_type: 'admission',
    encounter_ref: 'ADM-1',
    encounter_date: '2026-08-05',
    status: 'linked',
    is_linked: true,
    linked_at: '2026-08-11 10:00:00',
    linked_by: 'MRD-7',
    link_method: 'abdm_portal',
    link_method_label: 'ABDM / NHA portal',
    link_reference: null,
    unlinked_at: null,
    unlinked_by: null,
    unlink_reason: null,
    notes: null,
    transmitted_to_abdm: false,
    ...overrides,
  }
}

function makeConsent(overrides: Partial<ConsentArtefact> = {}): ConsentArtefact {
  const status = overrides.status ?? 'granted'
  return {
    id: 21,
    artefact_id: 'CM-ART-1',
    request_id: null,
    patient_id: 'PAT-1',
    patient_name: 'Meera Rajan',
    abha_identity_id: 1,
    requester_name: 'City Care HIU',
    requester_type: 'hiu',
    purpose_code: 'CAREMGT',
    purpose_label: 'Care management',
    purpose_text: null,
    hi_types: ['DischargeSummary'],
    hi_types_recorded: true,
    date_range_from: '2026-01-01',
    date_range_to: '2026-12-31',
    expires_at: '2027-01-01 00:00:00',
    expires_at_display: '2027-01-01 00:00',
    days_to_expiry: 142,
    status,
    effective_status: status,
    is_expired: false,
    is_effective: status === 'granted',
    is_revoked: status === 'revoked',
    can_be_reinstated: false,
    granted_at: '2026-08-11 10:00:00',
    denied_at: null,
    denial_reason: null,
    revoked_at: null,
    revoked_by: null,
    revocation_reason: null,
    recorded_where: 'consent_manager',
    recorded_by: 'ADM-1',
    notes: null,
    transmitted_to_abdm: false,
    ...overrides,
  }
}

function primeHappyPath(overrides: {
  queue?: QueueEntry[]
  identities?: AbhaIdentity[]
  contexts?: CareContext[]
  consents?: ConsentArtefact[]
} = {}) {
  const entries = overrides.queue ?? [makeQueueEntry()]
  api.getQueue.mockResolvedValue({
    ...wrapped,
    encounters: entries,
    total: entries.length,
    counts: { ready_to_link: 0, abha_unverified: entries.length, no_abha: 0 },
    buckets: [
      { id: 'ready_to_link', label: 'Verified ABHA — link it', count: 0 },
      { id: 'abha_unverified', label: 'ABHA recorded but not verified', count: entries.length },
      { id: 'no_abha', label: 'No ABHA on record', count: 0 },
    ],
    stale_after_days: 7,
  })
  api.getSummary.mockResolvedValue({
    ...wrapped,
    identities: {
      total: 1, verified: 0, unverified: 1, verification_failed: 0, deactivated: 0,
      patients_with_abha: 1, verified_of_total: '0 of 1',
    },
    care_contexts: { linked: 1, unlinked: 0 },
    consents: { requested: 0, granted: 1, denied: 0, expired: 0, revoked: 0 },
    queue: { total: entries.length, counts: { ready_to_link: 0, abha_unverified: 1, no_abha: 0 } },
  })
  api.getIdentities.mockResolvedValue({
    ...wrapped,
    identities: overrides.identities ?? [makeIdentity()],
    total: 1,
  })
  api.getCareContexts.mockResolvedValue({
    ...wrapped,
    care_contexts: overrides.contexts ?? [makeCareContext()],
    total: 1,
  })
  api.getConsents.mockResolvedValue({
    ...wrapped,
    consents: overrides.consents ?? [makeConsent()],
    total: 1,
  })
  api.getMeta.mockResolvedValue({
    ...wrapped,
    verification_statuses: ['unverified', 'verified', 'verification_failed', 'deactivated'],
    verification_methods: [{ code: 'aadhaar_otp', label: 'Aadhaar OTP on the ABDM portal' }],
    verification_outcomes: ['verified', 'failed', 'inconclusive'],
    verification_places: [{ code: 'abdm_portal', label: 'ABDM / NHA portal' }],
    identity_sources: [{ code: 'counter_keyed', label: 'Keyed at the registration counter' }],
    hi_types: ['OPConsultation', 'DischargeSummary'],
    link_methods: [{ code: 'abdm_portal', label: 'ABDM / NHA portal' }],
    encounter_types: ['admission'],
    consent_statuses: ['requested', 'granted', 'denied', 'expired', 'revoked'],
    consent_transitions: { granted: ['revoked', 'expired'], revoked: [] },
    purpose_codes: [{ code: 'CAREMGT', label: 'Care management' }],
    requester_types: ['hiu'],
    queue_buckets: [{ id: 'ready_to_link', label: 'Verified ABHA — link it' }],
    recognised_abha_suffixes: ['abdm', 'sbx'],
    checks_abha_check_digit: false,
    milestone_status: 'Orb records ABDM steps performed elsewhere.',
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  primeHappyPath()
})

afterEach(() => {
  /* RTL's automatic cleanup unmounts the tree, which is what removes the
     PORTALLED modal from document.body. Wiping innerHTML here instead would rip
     the portal out from under React and turn every modal test into
     "The node to be removed is not a child of this node". */
  cleanup()
})

/* ═══════════════════════════════════════════════════════════════════════════
   1. THE HONESTY
   ═══════════════════════════════════════════════════════════════════════════ */

describe('the no-gateway notice', () => {
  it('is on screen before any fetch resolves', () => {
    /* Every call left pending: this is the FIRST PAINT, and the sentence has to
       be there already. */
    api.getQueue.mockReturnValue(new Promise(() => {}))
    api.getSummary.mockReturnValue(new Promise(() => {}))
    api.getIdentities.mockReturnValue(new Promise(() => {}))
    api.getCareContexts.mockReturnValue(new Promise(() => {}))
    api.getConsents.mockReturnValue(new Promise(() => {}))
    api.getMeta.mockReturnValue(new Promise(() => {}))

    render(<AbdmPanel />)
    expect(
      screen.getByText(/Orb is not connected to the ABDM gateway/),
    ).toBeTruthy()
  })

  it('is still on screen when every call fails', async () => {
    const boom = () => Promise.reject(new Error('network down'))
    api.getQueue.mockImplementation(boom)
    api.getSummary.mockImplementation(boom)
    api.getIdentities.mockImplementation(boom)
    api.getCareContexts.mockImplementation(boom)
    api.getConsents.mockImplementation(boom)
    api.getMeta.mockImplementation(boom)

    render(<AbdmPanel />)
    await waitFor(() => expect(screen.getByText(/network down/)).toBeTruthy())
    expect(screen.getByText(/Orb is not connected to the ABDM gateway/)).toBeTruthy()
  })

  it('prefers the server copy once it has one, and they say the same thing', async () => {
    render(<AbdmPanel />)
    expect(await screen.findByText(SERVER_NOTICE)).toBeTruthy()
  })

  it('never claims a transmission happened', async () => {
    render(<AbdmPanel />)
    await screen.findByText('Meera Rajan')
    const body = document.body.textContent ?? ''
    for (const lie of ['Sent to ABDM', 'Submitted to ABDM', 'Synced', 'Uploading']) {
      expect(body).not.toContain(lie)
    }
  })
})

/* ═══════════════════════════════════════════════════════════════════════════
   2. UNVERIFIED NEVER LOOKS VERIFIED
   ═══════════════════════════════════════════════════════════════════════════ */

describe('an unverified ABHA', () => {
  it('carries the words "Not verified" beside the number', async () => {
    render(<AbdmPanel />)
    await click(await findButton('ABHA records'))

    const row = (await screen.findByText('12-3456-7890-1234')).closest('li') as HTMLElement
    expect(within(row).getByText('Not verified')).toBeTruthy()
  })

  it('is not offered a link action in the queue', async () => {
    render(<AbdmPanel />)
    /* The unverified bucket offers "Record verification", never "Record link" —
       linking a mistyped national health id publishes this patient's record
       into somebody else's account. */
    expect(await findButton('Record verification')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Record link' })).toBeNull()
  })

  it('refuses to submit a link even if the modal is reached', async () => {
    /* A verified-looking queue entry whose identity is actually unverified —
       the drift a stale list could produce. The confirm button stays disabled
       and the warning is shown. */
    primeHappyPath({
      queue: [makeQueueEntry({ bucket: 'ready_to_link', bucket_label: 'Verified ABHA — link it' })],
      identities: [makeIdentity({ verification_status: 'unverified' })],
    })
    render(<AbdmPanel />)
    await click(await findButton('Record link'))
    expect(screen.getByText(/linking a mistyped number/i)).toBeTruthy()
    const confirm = screen.getByRole('button', { name: 'Record this link' }) as HTMLButtonElement
    expect(confirm.disabled).toBe(true)
    expect(api.linkCareContext).not.toHaveBeenCalled()
  })

  it('says out loud that what is recorded is unverified', async () => {
    primeHappyPath({
      queue: [makeQueueEntry({
        bucket: 'no_abha', bucket_label: 'No ABHA on record',
        abha_identity_id: null, abha_number: null, abha_number_formatted: null,
        verification_status: null, verification_label: 'No ABHA on record',
      })],
    })
    render(<AbdmPanel />)
    await click(await findButton('Record ABHA'))
    /* Split across a <strong>, so the assertion is on the text node plus the
       emphasised word, not on a regex that spans elements. */
    expect(screen.getByText(/What you record here is stored/i)).toBeTruthy()
    expect(screen.getByText('unverified')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Record as unverified' })).toBeTruthy()
  })
})

/* ═══════════════════════════════════════════════════════════════════════════
   3. AN UNLINK IS NEVER SILENT
   ═══════════════════════════════════════════════════════════════════════════ */

describe('unlinking a care context', () => {
  it('cannot be confirmed without a reason', async () => {
    render(<AbdmPanel />)
    await click(await findButton('Care contexts'))
    await click(await findButton('Record unlink'))

    const confirm = screen.getByRole('button', { name: 'Record this unlink' }) as HTMLButtonElement
    expect(confirm.disabled).toBe(true)
    await click(confirm)
    expect(api.unlinkCareContext).not.toHaveBeenCalled()

    fireEvent.change(screen.getByRole('textbox'), {
      target: { value: 'Linked against the wrong episode' },
    })
    expect((screen.getByRole('button', { name: 'Record this unlink' }) as HTMLButtonElement).disabled)
      .toBe(false)
  })

  it('warns that the record is kept, not deleted', async () => {
    render(<AbdmPanel />)
    await click(await findButton('Care contexts'))
    await click(await findButton('Record unlink'))

    expect(screen.getByText(/kept, not deleted/i)).toBeTruthy()
  })

  it('sends the reason and refreshes', async () => {
    api.unlinkCareContext.mockResolvedValue({
      ...wrapped, care_context: makeCareContext({ status: 'unlinked', is_linked: false }),
    })
    render(<AbdmPanel />)
    await click(await findButton('Care contexts'))
    await click(await findButton('Record unlink'))

    fireEvent.change(screen.getByRole('textbox'), {
      target: { value: 'Episode merged into the readmission' },
    })
    await click(screen.getByRole('button', { name: 'Record this unlink' }))

    expect(api.unlinkCareContext).toHaveBeenCalledWith(11, 'Episode merged into the readmission')
  })

  it('an unlinked row still shows it was linked, and by whom it was undone', async () => {
    primeHappyPath({
      contexts: [makeCareContext({
        status: 'unlinked', is_linked: false,
        unlinked_at: '2026-08-12 09:00:00', unlinked_by: 'ADM-1',
        unlink_reason: 'Episode merged into the readmission',
      })],
    })
    render(<AbdmPanel />)
    await click(await findButton('Care contexts'))

    const row = (await screen.findByText('Admission 5 Aug')).closest('li') as HTMLElement
    expect(within(row).getByText('Unlinked')).toBeTruthy()
    expect(within(row).getByText(/Episode merged into the readmission/)).toBeTruthy()
    // The row is still there at all — an unlink is not a deletion.
    expect(within(row).getByText(/ABDM \/ NHA portal/)).toBeTruthy()
  })
})

/* ═══════════════════════════════════════════════════════════════════════════
   4. A REVOKED CONSENT OFFERS NO WAY BACK
   ═══════════════════════════════════════════════════════════════════════════ */

describe('consent revocation', () => {
  it('warns that it cannot be undone, and needs a reason', async () => {
    render(<AbdmPanel />)
    await click(await findButton('Consent artefacts'))
    await click(await findButton('Record revocation'))

    expect(screen.getByText(/cannot be undone/i)).toBeTruthy()
    expect(screen.getByText(/NEW artefact/)).toBeTruthy()
    const confirm = screen.getByRole('button', { name: 'Record this revocation' }) as HTMLButtonElement
    expect(confirm.disabled).toBe(true)
    expect(api.revokeConsent).not.toHaveBeenCalled()
  })

  it('offers no reinstate control on a revoked artefact', async () => {
    primeHappyPath({
      consents: [makeConsent({
        status: 'revoked', effective_status: 'revoked', is_revoked: true,
        is_effective: false, revoked_by: 'ADM-1',
        revocation_reason: 'Patient withdrew at the counter',
      })],
    })
    render(<AbdmPanel />)
    await click(await findButton('Consent artefacts'))
    await screen.findByText(/cannot be reinstated/i)

    for (const name of [/reinstate/i, /restore/i, /re-grant/i, /Record revocation/]) {
      expect(screen.queryByRole('button', { name })).toBeNull()
    }
    expect(screen.getByText(/cannot be reinstated/i)).toBeTruthy()
    expect(screen.getByText(/Patient withdrew at the counter/)).toBeTruthy()
  })

  it('never tells the desk to record a new artefact the screen cannot record', async () => {
    /* The desk has no control to record a consent artefact. "Record the new
       artefact instead" sent staff looking for one. */
    primeHappyPath({
      consents: [makeConsent({
        status: 'revoked', effective_status: 'revoked', is_revoked: true,
        is_effective: false, revoked_by: 'ADM-1',
        revocation_reason: 'Patient withdrew at the counter',
      })],
    })
    render(<AbdmPanel />)
    await click(await findButton('Consent artefacts'))
    const note = await screen.findByText(/cannot be reinstated/i)
    expect(note.textContent).toMatch(/this screen cannot record one yet/i)
    expect(document.body.textContent).not.toMatch(/record the new artefact/i)
    expect(screen.queryByRole('button', { name: /record.*(consent|artefact)/i })).toBeNull()
  })

  it('the revocation warning does not promise a way to record the new artefact', async () => {
    render(<AbdmPanel />)
    await click(await findButton('Consent artefacts'))
    await click(await findButton('Record revocation'))
    const warning = screen.getByText(/NEW artefact/)
    expect(warning.textContent).toMatch(/this screen cannot record one yet/i)
    expect(warning.textContent).not.toMatch(/you record that one/i)
  })

  it('renders the EFFECTIVE status, not the stored one, when they disagree', async () => {
    /* A stored 'granted' whose expiry has passed must never read as live
       permission. Both are shown so nobody has to wonder if the screen is stale. */
    primeHappyPath({
      consents: [makeConsent({
        status: 'granted', effective_status: 'expired', is_expired: true, is_effective: false,
      })],
    })
    render(<AbdmPanel />)
    await click(await findButton('Consent artefacts'))

    const row = (await screen.findByText('City Care HIU')).closest('li') as HTMLElement
    expect(within(row).getByText('Expired')).toBeTruthy()
    expect(within(row).getByText(/stored: Granted by patient/)).toBeTruthy()
  })
})

describe('stored times are UTC and render in local time', () => {
  it('prints the ABHA record time and the consent expiry through the clinical formatter', async () => {
    // Both columns are bare UTC strings. Printed raw, a record made at 02:30 PM
    // IST read "09:00:00", and the expiry carried the UTC clock with no zone.
    render(<AbdmPanel />)
    await click(await findButton('ABHA records'))
    const row = (await screen.findByText('12-3456-7890-1234')).closest('li') as HTMLElement
    expect(row.textContent).toContain(
      `recorded ${formatClinicalDateTime(new Date(Date.UTC(2026, 7, 10, 9, 0)))}`)
    expect(row.textContent).not.toContain('2026-08-10 09:00:00')

    await click(await findButton('Consent artefacts'))
    const consent = (await screen.findByText('City Care HIU')).closest('li') as HTMLElement
    expect(consent.textContent).toContain(
      `Expires ${formatClinicalDateTime(new Date(Date.UTC(2027, 0, 1, 0, 0)))}`)
  })
})

/* ═══════════════════════════════════════════════════════════════════════════
   5. EVERY DIALOG NAMES THE PATIENT
   A national health id recorded against the wrong row is a wrong-patient
   error that leaves the hospital. The dialogs used to say "Patient PAT-1".
   ═══════════════════════════════════════════════════════════════════════════ */

describe('the ABDM dialogs name the patient', () => {
  it('Record an ABHA shows name, MRN and ward, not the internal id', async () => {
    primeHappyPath({
      queue: [makeQueueEntry({
        bucket: 'no_abha', bucket_label: 'No ABHA on record',
        abha_identity_id: null, abha_number: null, abha_number_formatted: null,
        verification_status: null, verification_label: 'No ABHA on record',
      })],
    })
    render(<AbdmPanel />)
    await click(await findButton('Record ABHA'))
    const dialog = screen.getByRole('dialog', { name: 'Record an ABHA' })
    expect(within(dialog).getByText('Meera Rajan, MRN-1, W1')).toBeTruthy()
    expect(dialog.textContent).not.toContain('Patient PAT-1')
  })

  it('Record a verification shows the patient beside the ABHA', async () => {
    render(<AbdmPanel />)
    await click(await findButton('Record verification'))
    const dialog = screen.getByRole('dialog', { name: 'Record a verification' })
    expect(within(dialog).getByText('Meera Rajan, MRN-1, W1 · ABHA 12-3456-7890-1234')).toBeTruthy()
  })

  it('sends the picked local time as UTC', async () => {
    api.recordVerification.mockResolvedValue({ ...wrapped, identity: makeIdentity() })
    render(<AbdmPanel />)
    await click(await findButton('Record verification'))
    const dialog = screen.getByRole('dialog', { name: 'Record a verification' })
    const when = dialog.querySelector('input[type="datetime-local"]') as HTMLInputElement
    expect(when).toBeTruthy()
    fireEvent.change(when, { target: { value: '2026-08-12T09:15' } })
    fireEvent.change(within(dialog).getByPlaceholderText('Employee id or name'), {
      target: { value: 'MRD-7' },
    })
    await click(within(dialog).getByRole('button', { name: 'Record this verification' }))
    expect(api.recordVerification).toHaveBeenCalledWith(1, expect.objectContaining({
      performed_at: new Date('2026-08-12T09:15').toISOString(),
    }))
  })

  it('closes on Escape through the shared focus trap', async () => {
    render(<AbdmPanel />)
    await click(await findButton('Record verification'))
    const dialog = screen.getByRole('dialog', { name: 'Record a verification' })
    await act(async () => {
      dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    })
    expect(screen.queryByRole('dialog', { name: 'Record a verification' })).toBeNull()
  })
})

/* ═══════════════════════════════════════════════════════════════════════════
   Gap review F105 / F157: a zero count carries no tone, and a health-
   information type is named as the record it is, not as its CamelCase code.
   ═══════════════════════════════════════════════════════════════════════════ */

describe('summary and labels', () => {
  it('does not colour a zero count, and does colour a non-zero one', async () => {
    primeHappyPath({ queue: [] })
    render(<AbdmPanel />)
    const label = await screen.findByText('Unverified')
    const unverified = label.parentElement!.querySelector('.abdm-metric__value')!
    expect(unverified.textContent).toBe('1')
    expect(unverified.className).toMatch(/--warn/)
    const notLinked = screen.getByText('Encounters not linked').parentElement!.querySelector('.abdm-metric__value')!
    expect(notLinked.textContent).toBe('0')
    expect(notLinked.className).not.toMatch(/--danger/)
  })

  it('names a health-information type in words', async () => {
    primeHappyPath()
    render(<AbdmPanel />)
    await click(await findButton('Care contexts'))
    const row = (await screen.findByText('Admission 5 Aug')).closest('li') as HTMLElement
    expect(within(row).getByText(/Discharge summary/)).toBeTruthy()
    expect(within(row).queryByText(/DischargeSummary/)).toBeNull()
  })
})
