/**
 * End of stay (adt-9) and the bed board's 'Record end of stay' action.
 *
 * What they must never get wrong:
 *
 * 1. A DEATH CANNOT BE SAVED UNTIL THE TYPED MRN MATCHES the one on file,
 *    compared as the server compares it (no spaces, no capital letters). The
 *    server's read_back_mismatch is shown in its own words, and a retry sends
 *    the same Idempotency-Key, so a lost reply never records the end twice.
 * 2. EACH TYPE POSTS THE RIGHT BODY TO THE RIGHT ROUTE: POST
 *    /api/adt/admissions/{id}/end with the end type, the typed MRN, the time
 *    and, for LAMA, DAMA and absconded, the reason they cannot be sent without.
 * 3. OPEN CHAINS ARE LISTED (F050), each closable with a reason, and never
 *    block the end (R009 held). A list that could not be read says so; it is
 *    never drawn as empty. Running care bundles are listed as cancelled.
 * 4. THE TIME stays inside the back-time window and out of the future.
 * 5. AFTERWARDS it says what was recorded, and 'Recorded in error' opens the
 *    reopen screen in place.
 * 6. THE TILE ACTION is offered only to a doctor (discharge.generate), only on
 *    an occupied tile whose occupant the caller may see.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

type FetchInit = { method?: string; body?: string; headers?: Record<string, string> }

const apiFetch = vi.fn<(path: string, init?: FetchInit) => Promise<unknown>>()
const getOpenEscalations = vi.fn<(patientId: string) => Promise<unknown[]>>()
const resolveEscalation = vi.fn<(...a: unknown[]) => Promise<unknown>>()

vi.mock('../../api/client', () => ({
  apiFetch: (path: string, init?: FetchInit) => apiFetch(path, init),
  errorDetailMessage: (detail: unknown, status: number) => {
    if (typeof detail === 'string') return detail
    const message = (detail as { message?: unknown } | null)?.message
    return typeof message === 'string' ? message : `The server answered ${status}.`
  },
  getOpenEscalations: (patientId: string) => getOpenEscalations(patientId),
  resolveEscalation: (...a: unknown[]) => resolveEscalation(...a),
}))

import EndStayDrawer from './EndStayDrawer'
import EndStayAction, * as endStayActionModule from './actions/EndStayAction'
import { actionsForTile, discoverTileActions, TILE_ACTIONS } from './actions'
import { toLocalInput } from '../../api/adt/endStay'
import { resetClinicalSwitches, setClinicalSwitches } from '../../shellConfig'
import type { BedTile, BoardWard } from '../../api/adt/board'

const PATIENT = { id: 'P021', name: 'Ravi Kumar', mrn: 'MRN-0021', ward: 'GM-2', bed: '21' }

function refusal(status: number, detail: unknown, message?: string) {
  return Object.assign(new Error(message ?? (detail as { message?: string })?.message ?? 'refused'),
    { status, detail })
}

const ENDED = {
  status: 'ended', replayed: false, admission_id: 'ADM-0021', patient_id: 'P021', end_type: 'death',
  admission_status: 'discharged', discharged_at: '2026-10-06T04:30:00Z', discharged_by: 'DOC1',
  movement_id: 'MOV-9', discharge_link_issued: false, bed_freed: 'BED-21', preconditions: [],
  disposition: { id: 7, disposition: 'lama', end_of_stay_disposition: 'death', source: 'admin',
                 action: 'matched_admission_flagged', needs_reconciliation: true },
  open_escalations: [{ id: 'ESC-1', status: 'open', raised_at: '2026-10-06T01:00:00Z' }],
  bundles_cancelled: [{ id: 3, bundle_type: 'sepsis6' }],
  portal_login_deactivated: true,
}

function routeFetch(over: Record<string, unknown> = {}) {
  apiFetch.mockImplementation(async (path: string) => {
    for (const [prefix, answer] of Object.entries(over)) {
      if (path.startsWith(prefix)) {
        if (answer instanceof Error) throw answer
        return typeof answer === 'function' ? (answer as () => unknown)() : answer
      }
    }
    if (path === '/adt/meta') return { module: 'adt', state: 'on', settings: { backtime_hours: 72 } }
    if (path.startsWith('/bundles/patient/')) return { bundles: [] }
    return {}
  })
}

function posts(prefix: string) {
  return apiFetch.mock.calls.filter(([p, init]) => p.startsWith(prefix) && init?.method === 'POST')
}

/** Renders the screen and lets its three reads (the module's settings, the
 *  open chains, the running bundles) answer before the test goes on. */
async function renderDrawer(props: Partial<Parameters<typeof EndStayDrawer>[0]> = {}) {
  const onDone = vi.fn()
  const onClose = vi.fn()
  await act(async () => {
    render(
      <EndStayDrawer admissionId="ADM-0021" patient={PATIENT} onDone={onDone} onClose={onClose} {...props} />,
    )
  })
  return { onDone, onClose }
}

const recordButton = () => screen.getByRole('button', { name: /^Record (death|end of stay)$/ })

beforeEach(() => {
  // Before anything mounts: a reset with a screen on it re-renders outside act.
  resetClinicalSwitches()
  apiFetch.mockReset()
  getOpenEscalations.mockReset()
  resolveEscalation.mockReset()
  getOpenEscalations.mockResolvedValue([])
  routeFetch()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('the read-back', () => {
  it('reads back the name, MRN, ward and bed, and offers only the ends with no summary', async () => {
    await renderDrawer()
    const readback = screen.getByText('Ward and bed').closest('dl') as HTMLElement
    expect(readback).toHaveTextContent('Ravi Kumar')
    expect(readback).toHaveTextContent('MRN-0021')
    expect(readback).toHaveTextContent('GM-2, bed 21')
    const group = screen.getByRole('group', { name: 'How the stay ended' })
    const choices = within(group).getAllByRole('radio').map((r) => (r as HTMLInputElement).value)
    expect(choices).toEqual(['lama', 'dama', 'absconded', 'death'])
    expect(within(group).getByText(/recorded from the chart with Discharge/)).toBeInTheDocument()
    // Nothing is chosen for the doctor.
    expect(within(group).getAllByRole('radio').some((r) => (r as HTMLInputElement).checked)).toBe(false)
    await waitFor(() => expect(getOpenEscalations).toHaveBeenCalledWith('P021'))
  })

  it('a death cannot be saved until the typed MRN matches, and then posts the right body', async () => {
    await renderDrawer({ initialEndType: 'death' })
    expect(screen.getByRole('radio', { name: 'Death' })).toBeChecked()
    expect(recordButton()).toHaveTextContent('Record death')
    expect(recordButton()).toBeDisabled()

    const mrn = screen.getByLabelText('Type the MRN from the wristband to confirm')
    fireEvent.change(mrn, { target: { value: 'MRN-0012' } })
    expect(screen.getByText('This does not match the MRN on file.')).toBeInTheDocument()
    expect(recordButton()).toBeDisabled()
    fireEvent.click(recordButton())
    expect(posts('/adt/')).toHaveLength(0)

    // The server's comparison: spaces and capitals do not matter.
    fireEvent.change(mrn, { target: { value: ' mrn-0021 ' } })
    expect(screen.getByText('Matches the MRN on file.')).toBeInTheDocument()
    expect(recordButton()).toBeEnabled()

    routeFetch({ '/adt/admissions/ADM-0021/end': ENDED })
    await act(async () => { fireEvent.click(recordButton()) })

    const [call] = posts('/adt/admissions/')
    expect(call[0]).toBe('/adt/admissions/ADM-0021/end')
    const body = JSON.parse(call[1]?.body ?? '{}')
    expect(body.end_type).toBe('death')
    expect(body.confirm_mrn).toBe('mrn-0021')
    expect(typeof body.occurred_at).toBe('string')
    expect(Number.isNaN(Date.parse(body.occurred_at))).toBe(false)
    expect(body).not.toHaveProperty('reason')
    expect(call[1]?.headers?.['Idempotency-Key']).toMatch(/^[0-9a-f-]{36}$/)
    expect(await screen.findByTestId('end-stay-result')).toBeInTheDocument()
  })

  it("shows the server's read_back_mismatch in words, and a retry sends the same key", async () => {
    routeFetch({
      '/adt/admissions/ADM-0021/end': refusal(409, {
        error: 'read_back_mismatch',
        message: "The MRN typed does not match this patient's record. Check the wristband and type it again. Nothing was recorded.",
      }),
    })
    await renderDrawer({ initialEndType: 'death' })
    fireEvent.change(screen.getByLabelText('Type the MRN from the wristband to confirm'), { target: { value: 'MRN-0021' } })
    await act(async () => { fireEvent.click(recordButton()) })
    expect(await screen.findByRole('alert')).toHaveTextContent(
      "The MRN typed does not match this patient's record. Check the wristband and type it again. Nothing was recorded.")
    expect(screen.queryByTestId('end-stay-result')).toBeNull()

    await act(async () => { fireEvent.click(recordButton()) })
    const [first, second] = posts('/adt/admissions/')
    expect(first[1]?.headers?.['Idempotency-Key']).toBeTruthy()
    expect(second[1]?.headers?.['Idempotency-Key']).toBe(first[1]?.headers?.['Idempotency-Key'])
  })

  it('a patient with no MRN on file can never be confirmed here, and says so', async () => {
    await renderDrawer({ initialEndType: 'death', patient: { ...PATIENT, mrn: null } })
    expect(screen.getByText(/No MRN is on file for this patient/)).toBeInTheDocument()
    expect(screen.getByLabelText('Type the MRN from the wristband to confirm')).toBeDisabled()
    expect(recordButton()).toBeDisabled()
  })
})

describe('each end type', () => {
  it.each([
    ['lama', 'Left against medical advice (LAMA)', true],
    ['dama', 'Discharged against medical advice (DAMA)', true],
    ['absconded', 'Absconded', true],
    ['death', 'Death', false],
  ] as const)('%s posts its own body to the end route', async (endType, label, needsReason) => {
    routeFetch({ '/adt/admissions/ADM-0021/end': { ...ENDED, end_type: endType } })
    await renderDrawer()
    fireEvent.click(screen.getByRole('radio', { name: label }))
    fireEvent.change(screen.getByLabelText('Type the MRN from the wristband to confirm'), { target: { value: 'MRN-0021' } })
    if (needsReason) {
      // Without the reason it cannot be sent.
      expect(recordButton()).toBeDisabled()
      fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Family taking him home' } })
    } else {
      expect(screen.queryByLabelText('Reason')).toBeNull()
    }
    fireEvent.change(screen.getByLabelText('Notes (optional)'), { target: { value: 'Seen by the registrar' } })
    expect(recordButton()).toBeEnabled()
    await act(async () => { fireEvent.click(recordButton()) })

    const calls = posts('/adt/')
    expect(calls).toHaveLength(1)
    expect(calls[0][0]).toBe('/adt/admissions/ADM-0021/end')
    const body = JSON.parse(calls[0][1]?.body ?? '{}')
    expect(body.end_type).toBe(endType)
    expect(body.confirm_mrn).toBe('MRN-0021')
    expect(body.notes).toBe('Seen by the registrar')
    if (needsReason) expect(body.reason).toBe('Family taking him home')
    else expect(body).not.toHaveProperty('reason')
  })
})

describe('the time', () => {
  it('refuses a time beyond the back-time window and one in the future', async () => {
    await renderDrawer({ initialEndType: 'death' })
    fireEvent.change(screen.getByLabelText('Type the MRN from the wristband to confirm'), { target: { value: 'MRN-0021' } })
    const when = screen.getByLabelText('When the stay ended')
    expect(screen.getByText('Up to 72 hours back, and not before the admission.')).toBeInTheDocument()

    fireEvent.change(when, { target: { value: toLocalInput(new Date(Date.now() - 80 * 3600 * 1000)) } })
    expect(screen.getByText(/can be recorded up to 72 hours back/)).toBeInTheDocument()
    expect(recordButton()).toBeDisabled()

    fireEvent.change(when, { target: { value: toLocalInput(new Date(Date.now() + 3 * 3600 * 1000)) } })
    expect(screen.getByText('The time the stay ended is in the future.')).toBeInTheDocument()
    expect(recordButton()).toBeDisabled()

    fireEvent.change(when, { target: { value: toLocalInput(new Date(Date.now() - 2 * 3600 * 1000)) } })
    expect(recordButton()).toBeEnabled()
  })

  it("takes the site's window from the module's settings", async () => {
    routeFetch({ '/adt/meta': { settings: { backtime_hours: 24 } } })
    await renderDrawer({ initialEndType: 'death' })
    expect(await screen.findByText('Up to 24 hours back, and not before the admission.')).toBeInTheDocument()
  })
})

describe('what is still open', () => {
  it('lists open chains, never blocks on them, and closes one with a reason', async () => {
    getOpenEscalations.mockResolvedValue([
      { id: 'ESC-1', patient_id: 'P021', status: 'open', raised_at: '2026-10-06 01:00:00', peak_news2: 7 },
      { id: 'ESC-2', patient_id: 'P021', status: 'acknowledged', raised_at: '2026-10-06 02:00:00' },
    ])
    resolveEscalation.mockResolvedValue({})
    await renderDrawer({ initialEndType: 'death' })
    const box = await screen.findByTestId('end-stay-escalations')
    await waitFor(() => expect(box).toHaveTextContent('2 deterioration escalations are still open'))
    expect(box).toHaveTextContent('Recording the end does not close them')
    expect(box).toHaveTextContent('highest NEWS2 7, not acknowledged')
    expect(box).toHaveTextContent('acknowledged')

    // They never hold up the end.
    fireEvent.change(screen.getByLabelText('Type the MRN from the wristband to confirm'), { target: { value: 'MRN-0021' } })
    expect(recordButton()).toBeEnabled()

    const [firstReason] = within(box).getAllByLabelText('Reason for closing')
    const [firstClose] = within(box).getAllByRole('button', { name: 'Close escalation' })
    expect(firstClose).toBeDisabled()
    fireEvent.change(firstReason, { target: { value: 'Patient died' } })
    await act(async () => { fireEvent.click(firstClose) })
    expect(resolveEscalation).toHaveBeenCalledWith('ESC-1', 'Patient died', 'patient_died')
    await waitFor(() => expect(box).toHaveTextContent('1 deterioration escalation is still open'))
  })

  it('prints no NEWS2 beside a chain while scoring is switched off (the Ward Record)', async () => {
    setClinicalSwitches({ scoring_enabled: false, ward_record: true })
    getOpenEscalations.mockResolvedValue([
      { id: 'ESC-1', patient_id: 'P021', status: 'open', raised_at: '2026-10-06 01:00:00', peak_news2: 7 },
    ])
    await renderDrawer({ initialEndType: 'death' })
    const box = await screen.findByTestId('end-stay-escalations')
    await waitFor(() => expect(box).toHaveTextContent('1 deterioration escalation is still open'))
    expect(box).not.toHaveTextContent('NEWS2')
  })

  it('says so when the open chains could not be read, never showing an empty list', async () => {
    getOpenEscalations.mockRejectedValue(new Error('offline'))
    await renderDrawer()
    const box = await screen.findByTestId('end-stay-escalations')
    await waitFor(() => expect(box).toHaveTextContent('Could not read the open escalations'))
    expect(box).not.toHaveTextContent('No deterioration escalation is open')
  })

  it('lists the running care bundles the end will cancel, and says so when it cannot read them', async () => {
    routeFetch({
      '/bundles/patient/P021': {
        bundles: [{ id: 3, patient_id: 'P021', bundle_type: 'sepsis6', bundle_label: 'Sepsis Six',
                    status: 'active', triggered_at: '2026-10-06 01:00:00' }],
      },
    })
    await renderDrawer()
    const box = await screen.findByTestId('end-stay-bundles')
    await waitFor(() => expect(box).toHaveTextContent('Cancelled when the stay ends'))
    expect(box).toHaveTextContent('Sepsis Six, started')
    expect(apiFetch.mock.calls.some(([p]) => p === '/bundles/patient/P021?active_only=true')).toBe(true)
  })

  it('says it could not read the bundles rather than showing none', async () => {
    routeFetch({ '/bundles/patient/P021': refusal(500, 'Internal error') })
    await renderDrawer()
    const box = await screen.findByTestId('end-stay-bundles')
    await waitFor(() => expect(box).toHaveTextContent('Could not read the care bundles'))
  })
})

describe('afterwards', () => {
  async function recordDeath() {
    routeFetch({ '/adt/admissions/ADM-0021/end': ENDED })
    const handlers = await renderDrawer({ initialEndType: 'death' })
    fireEvent.change(screen.getByLabelText('Type the MRN from the wristband to confirm'), { target: { value: 'MRN-0021' } })
    await act(async () => { fireEvent.click(recordButton()) })
    return handlers
  }

  it('says what was recorded, what was cancelled and what is still open', async () => {
    const { onDone } = await recordDeath()
    const result = await screen.findByTestId('end-stay-result')
    expect(within(result).getByRole('heading', { name: 'Death recorded' })).toBeInTheDocument()
    expect(result).toHaveTextContent('Death, recorded for')
    expect(result).toHaveTextContent('No discharge summary was drafted and no discharge link was issued.')
    expect(result).toHaveTextContent('The bed is free for the next patient.')
    expect(result).toHaveTextContent('already recorded this stay')
    expect(result).toHaveTextContent('Left against medical advice (LAMA)')
    expect(result).toHaveTextContent("portal login is switched off")
    expect(result).toHaveTextContent('Sepsis Six')
    expect(within(result).getByTestId('end-stay-still-open')).toHaveTextContent('1 deterioration escalation is still open')
    fireEvent.click(within(result).getByRole('button', { name: 'Done' }))
    expect(onDone).toHaveBeenCalledWith({ ended: ENDED, reopened: null })
  })

  it("'Recorded in error' opens the reopen screen in place and reopens the stay", async () => {
    const { onDone } = await recordDeath()
    fireEvent.click(await screen.findByRole('button', { name: 'Recorded in error' }))
    const dialog = screen.getByTestId('reopen-stay')
    expect(dialog).toHaveTextContent('Recorded as')
    expect(dialog).toHaveTextContent('Death')

    const reopened = {
      status: 'reopened', replayed: false, admission_id: 'ADM-0021', patient_id: 'P021',
      movement_id: 'MOV-10', corrects_movement_id: 'MOV-9', corrected_end_type: 'death',
      corrected_discharged_at: '2026-10-06T04:30:00Z', ward: 'GM-2', bed: '21', bed_retaken: true,
      awaiting_bed: false, portal_login_restored: true, bundles_not_restored: [],
    }
    routeFetch({ '/adt/admissions/ADM-0021/reopen': reopened })
    fireEvent.change(within(dialog).getByLabelText('Why the end was recorded in error'), { target: { value: 'Wrong patient' } })
    fireEvent.change(within(dialog).getByLabelText('Type the MRN from the wristband to confirm'), { target: { value: 'MRN-0021' } })
    await act(async () => { fireEvent.click(within(dialog).getByRole('button', { name: 'Reopen stay' })) })
    const [call] = posts('/adt/admissions/ADM-0021/reopen')
    expect(JSON.parse(call[1]?.body ?? '{}')).toEqual({ reason: 'Wrong patient', confirm_mrn: 'MRN-0021' })
    fireEvent.click(await screen.findByRole('button', { name: 'Done' }))
    expect(onDone).toHaveBeenCalledWith({ ended: ENDED, reopened })
  })

  it('Cancel before anything is recorded closes without a write', async () => {
    const { onClose, onDone } = await renderDrawer({ initialEndType: 'death' })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onClose).toHaveBeenCalled()
    expect(onDone).not.toHaveBeenCalled()
    expect(posts('/adt/')).toHaveLength(0)
  })
})

describe('the bed board action', () => {
  function tile(over: Partial<BedTile> = {}): BedTile {
    return {
      bed_id: 'BED-21', unit_code: 'GM-2/21', bed_label: '21', ward_id: 'WRD-2', room_id: null, room: null,
      room_name: null, bed_class: null, isolation: false, temporary: false, status: 'occupied',
      status_words: 'Occupied', tone: 'occupied', markers: [], free: false, housekeeping_state: null,
      since: null, minutes_in_state: null, occupant_hidden: false,
      occupant: { admission_id: 'ADM-0021', patient_id: 'P021', name: 'Ravi Kumar', mrn: 'MRN-0021',
                  department: 'General Medicine', boarder: false, boarder_from: null,
                  presence_status: null, linked: true },
      ...over,
    }
  }
  const WARD = { id: 'WRD-2', code: 'GM-2', name: null, department: 'General Medicine', ward_type: null,
                 is_active: true, cleaning_tracked: true, register_complete: true, counts: {}, tiles: [] } as BoardWard

  it("is installed as 'Record end of stay', for doctors, on occupied tiles they may see", () => {
    const entry = TILE_ACTIONS.find((a) => a.id === 'end-stay')
    expect(entry).toBeTruthy()
    expect(entry?.label).toBe('Record end of stay')
    expect(entry?.permission).toBe('discharge.generate')
    expect(entry?.needsOccupant).toBe(true)

    const entries = discoverTileActions({ './EndStayAction.tsx': endStayActionModule })
    const doctor = (p: string) => p === 'discharge.generate' || p === 'adt.read'
    const clerk = (p: string) => p === 'adt.read' || p === 'adt.bed'
    expect(actionsForTile(entries, tile(), doctor).map((a) => a.id)).toEqual(['end-stay'])
    for (const status of ['occupied_on_leave', 'occupied_absconded', 'boarder']) {
      expect(actionsForTile(entries, tile({ status }), doctor)).toHaveLength(1)
    }
    expect(actionsForTile(entries, tile(), clerk)).toHaveLength(0)
    expect(actionsForTile(entries, tile({ occupant: null, occupant_hidden: true }), doctor)).toHaveLength(0)
    expect(actionsForTile(entries, tile({ status: 'available', occupant: null }), doctor)).toHaveLength(0)
    expect(actionsForTile(entries, tile({ status: 'needs_cleaning', occupant: null }), doctor)).toHaveLength(0)
  })

  it('reads back the patient in the bed and reloads the board when done', async () => {
    routeFetch({ '/adt/admissions/ADM-0021/end': { ...ENDED, end_type: 'absconded' } })
    const onDone = vi.fn()
    const onClose = vi.fn()
    await act(async () => { render(<EndStayAction tile={tile()} ward={WARD} onDone={onDone} onClose={onClose} />) })
    const readback = screen.getByText('Ward and bed').closest('dl') as HTMLElement
    expect(readback).toHaveTextContent('Ravi Kumar')
    expect(readback).toHaveTextContent('GM-2, bed 21')
    fireEvent.click(screen.getByRole('radio', { name: 'Absconded' }))
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Not found on the ward since 02:00' } })
    fireEvent.change(screen.getByLabelText('Type the MRN from the wristband to confirm'), { target: { value: 'MRN-0021' } })
    await act(async () => { fireEvent.click(recordButton()) })
    expect(posts('/adt/admissions/ADM-0021/end')).toHaveLength(1)
    fireEvent.click(await screen.findByRole('button', { name: 'Done' }))
    expect(onDone).toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })

  // The board draws the open action in one unkeyed place and every tile's
  // buttons stay live while it is open, so a second bed re-renders the SAME
  // action with a new tile. Nothing typed for the first patient may reach the
  // second patient's record.
  const ALPHA = tile({
    bed_id: 'BED-1', unit_code: 'GM-2/1', bed_label: '1',
    occupant: { admission_id: 'ADM-A', patient_id: 'PA', name: 'Patient Alpha', mrn: 'MRN-A',
                department: 'General Medicine', boarder: false, boarder_from: null,
                presence_status: null, linked: true },
  })
  const BRAVO = tile({
    bed_id: 'BED-2', unit_code: 'GM-2/2', bed_label: '2',
    occupant: { admission_id: 'ADM-B', patient_id: 'PB', name: 'Patient Bravo', mrn: 'MRN-B',
                department: 'General Medicine', boarder: false, boarder_from: null,
                presence_status: null, linked: true },
  })
  const mrnField = () => screen.getByLabelText('Type the MRN from the wristband to confirm') as HTMLInputElement
  const notesField = () => screen.getByLabelText('Notes (optional)') as HTMLTextAreaElement
  const key = (call: [path: string, init?: FetchInit]) => call[1]?.headers?.['Idempotency-Key']

  it('a second bed opens a fresh screen: nothing chosen or typed carries over, and a new key is sent', async () => {
    // Alpha's reply is lost, so Alpha's screen keeps everything typed and its key.
    routeFetch({ '/adt/admissions/ADM-A/end': new Error('Failed to fetch') })
    const onDone = vi.fn()
    const onClose = vi.fn()
    let view: ReturnType<typeof render> | undefined
    await act(async () => { view = render(<EndStayAction tile={ALPHA} ward={WARD} onDone={onDone} onClose={onClose} />) })
    fireEvent.click(screen.getByRole('radio', { name: 'Death' }))
    fireEvent.change(notesField(), { target: { value: 'Notes written for Alpha' } })
    fireEvent.change(mrnField(), { target: { value: 'MRN-A' } })
    await act(async () => { fireEvent.click(recordButton()) })
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not reach Orb')
    const [alphaCall] = posts('/adt/admissions/ADM-A/end')
    expect(key(alphaCall)).toBeTruthy()

    // Bravo's tile is pressed while Alpha's screen is still open.
    await act(async () => { view?.rerender(<EndStayAction tile={BRAVO} ward={WARD} onDone={onDone} onClose={onClose} />) })
    const readback = screen.getByText('Ward and bed').closest('dl') as HTMLElement
    expect(readback).toHaveTextContent('Patient Bravo')
    expect(readback).toHaveTextContent('MRN-B')
    expect(readback).toHaveTextContent('GM-2, bed 2')
    expect(readback).not.toHaveTextContent('Patient Alpha')
    const group = screen.getByRole('group', { name: 'How the stay ended' })
    expect(within(group).getAllByRole('radio').some((r) => (r as HTMLInputElement).checked)).toBe(false)
    expect(notesField().value).toBe('')
    expect(mrnField().value).toBe('')
    expect(screen.queryByRole('alert')).toBeNull()
    expect(recordButton()).toBeDisabled()
    await waitFor(() => expect(getOpenEscalations).toHaveBeenCalledWith('PB'))

    routeFetch({ '/adt/admissions/ADM-B/end': { ...ENDED, admission_id: 'ADM-B', patient_id: 'PB' } })
    fireEvent.click(screen.getByRole('radio', { name: 'Death' }))
    fireEvent.change(mrnField(), { target: { value: 'MRN-B' } })
    await act(async () => { fireEvent.click(recordButton()) })
    const bravoCalls = posts('/adt/admissions/ADM-B/end')
    expect(bravoCalls).toHaveLength(1)
    const body = JSON.parse(bravoCalls[0][1]?.body ?? '{}')
    expect(body.end_type).toBe('death')
    expect(body.confirm_mrn).toBe('MRN-B')
    expect(body).not.toHaveProperty('notes')
    expect(key(bravoCalls[0])).toBeTruthy()
    expect(key(bravoCalls[0])).not.toBe(key(alphaCall))
    expect(posts('/adt/admissions/ADM-A/end')).toHaveLength(1)
  })

  it("one bed's recorded death does not stay up under the next bed", async () => {
    routeFetch({ '/adt/admissions/ADM-A/end': { ...ENDED, admission_id: 'ADM-A', patient_id: 'PA' } })
    const onDone = vi.fn()
    const onClose = vi.fn()
    let view: ReturnType<typeof render> | undefined
    await act(async () => { view = render(<EndStayAction tile={ALPHA} ward={WARD} onDone={onDone} onClose={onClose} />) })
    fireEvent.click(screen.getByRole('radio', { name: 'Death' }))
    fireEvent.change(mrnField(), { target: { value: 'MRN-A' } })
    await act(async () => { fireEvent.click(recordButton()) })
    expect(await screen.findByTestId('end-stay-result')).toBeInTheDocument()

    await act(async () => { view?.rerender(<EndStayAction tile={BRAVO} ward={WARD} onDone={onDone} onClose={onClose} />) })
    expect(screen.queryByTestId('end-stay-result')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Recorded in error' })).toBeNull()
    expect(screen.getByTestId('end-stay')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'End of stay' })).toBeInTheDocument()
    expect(screen.getByText('Ward and bed').closest('dl')).toHaveTextContent('Patient Bravo')
    expect(posts('/adt/admissions/ADM-B/')).toHaveLength(0)
    expect(onDone).not.toHaveBeenCalled()
  })

  it('a board refresh of the same stay keeps what was typed and the same key', async () => {
    routeFetch({ '/adt/admissions/ADM-A/end': new Error('Failed to fetch') })
    const onDone = vi.fn()
    const onClose = vi.fn()
    let view: ReturnType<typeof render> | undefined
    await act(async () => { view = render(<EndStayAction tile={ALPHA} ward={WARD} onDone={onDone} onClose={onClose} />) })
    fireEvent.click(screen.getByRole('radio', { name: 'Absconded' }))
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Not found on the ward since 02:00' } })
    fireEvent.change(notesField(), { target: { value: 'Security told' } })
    fireEvent.change(mrnField(), { target: { value: 'MRN-A' } })
    await act(async () => { fireEvent.click(recordButton()) })

    // The board reloads: a new tile object for the same stay.
    const refreshed = { ...ALPHA, minutes_in_state: 12, status_words: 'Occupied', occupant: { ...ALPHA.occupant! } }
    await act(async () => { view?.rerender(<EndStayAction tile={refreshed} ward={WARD} onDone={onDone} onClose={onClose} />) })
    expect(screen.getByRole('radio', { name: 'Absconded' })).toBeChecked()
    expect((screen.getByLabelText('Reason') as HTMLTextAreaElement).value).toBe('Not found on the ward since 02:00')
    expect(notesField().value).toBe('Security told')
    expect(mrnField().value).toBe('MRN-A')

    await act(async () => { fireEvent.click(recordButton()) })
    const [first, second] = posts('/adt/admissions/ADM-A/end')
    expect(key(first)).toBeTruthy()
    expect(key(second)).toBe(key(first))
  })
})

describe('words', () => {
  it("a key the server saw for another stay is said in plain words, never as the header's name", async () => {
    routeFetch({
      '/adt/admissions/ADM-0021/end': refusal(409, {
        error: 'idempotency_key_reused',
        message: 'That Idempotency-Key was already used for something else',
      }),
    })
    await renderDrawer({ initialEndType: 'death' })
    fireEvent.change(screen.getByLabelText('Type the MRN from the wristband to confirm'), { target: { value: 'MRN-0021' } })
    await act(async () => { fireEvent.click(recordButton()) })
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(
      "This screen's request was already used for a different stay, so nothing was recorded. "
      + 'Close this screen and open it again for this patient.')
    expect(alert).not.toHaveTextContent(/Idempotency/i)
  })

  it('no rendered sentence carries an em dash', async () => {
    getOpenEscalations.mockResolvedValue([{ id: 'ESC-1', patient_id: 'P021', status: 'open', raised_at: '2026-10-06 01:00:00' }])
    await renderDrawer({ initialEndType: 'lama' })
    await screen.findByText(/deterioration escalation is still open/)
    expect(document.body.textContent).not.toMatch(/\u2014/)
    routeFetch({ '/adt/admissions/ADM-0021/end': ENDED })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Went home' } })
    fireEvent.change(screen.getByLabelText('Type the MRN from the wristband to confirm'), { target: { value: 'MRN-0021' } })
    await act(async () => { fireEvent.click(recordButton()) })
    await screen.findByTestId('end-stay-result')
    expect(document.body.textContent).not.toMatch(/\u2014/)
  })
})
