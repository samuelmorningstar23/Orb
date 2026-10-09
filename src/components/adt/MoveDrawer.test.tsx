/**
 * The Move screen (adt-6a) and the bed board's 'Move patient' action.
 *
 * What they must never get wrong:
 *
 * 1. EACH MOVE POSTS THE RIGHT BODY TO THE RIGHT ROUTE. A bed change goes to
 *    POST /api/adt/admissions/{id}/bed-change and never names a department; a
 *    transfer goes to POST /api/adt/transfers with the receiving department.
 *    Both carry the bed the screen read when it opened (expected_bed_id), a
 *    reason, and one Idempotency-Key per screen, so a retry never records the
 *    move twice.
 * 2. A TRANSFER SAYS IT HANDS THE CHART OVER before the button, and only
 *    departments with clinical cover are offered.
 * 3. A BOARDER IS SAID IN WORDS AND CONFIRMED, whether the patient goes to a
 *    ward of another department or stays where they lie. Staying, a refusal
 *    is printed in an alert (no picker is on screen to hold it), and a
 *    boarder or a bed out of service the server names opens the question it
 *    asks, so its words are never a dead end; the place read's kept_place
 *    asks for both before the first press.
 * 4. A BED CHANGE ALWAYS NAMES A BED: 'No bed yet' is not offered.
 * 5. ONLY THE MOVES THE ROLE HOLDS ARE OFFERED (the ward clerk changes beds,
 *    never transfers), and the server's refusal is printed in its words.
 * 6. A SIGNING PIN typed for a terminal-clean authorisation is cleared after
 *    the send.
 * 7. A READ THAT FAILED SAYS SO, with a way to try again.
 */
import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

type FetchInit = { method?: string; body?: string; headers?: Record<string, string> }

const apiFetch = vi.fn<(path: string, init?: FetchInit) => Promise<unknown>>()
let role = 'doctor'

vi.mock('../../api/client', () => ({
  apiFetch: (path: string, init?: FetchInit) => apiFetch(path, init),
  errorDetailMessage: (detail: unknown, status: number) => {
    if (typeof detail === 'string') return detail
    const message = (detail as { message?: unknown } | null)?.message
    return typeof message === 'string' ? message : `The server answered ${status}.`
  },
  getUser: () => ({ role }),
}))

import MoveDrawer from './MoveDrawer'
import * as moveActionModule from './actions/MoveAction'
import MoveAction from './actions/MoveAction'
import { actionsForTile, discoverTileActions, TILE_ACTIONS } from './actions'
import type { BedTile, BoardWard } from '../../api/adt/board'
import type { MasterBedChoice, MoveOptions } from '../../api/adt/moves'

function bed(over: Partial<MasterBedChoice>): MasterBedChoice {
  return {
    bed_id: 'BED-1', bed_label: '1', unit_code: 'GM-1/1', room: null, bed_class: null,
    isolation: false, temporary: false, status: 'available', status_words: 'Available',
    tone: 'ready', markers: [], occupied: false, selectable: true, needs: null,
    terminal_clean: false, ...over,
  }
}

const OPTIONS: MoveOptions = {
  departments: ['General Medicine', 'Intensive Care'],
  master: {
    backtime_hours: 72,
    self_authoriser: null,
    authorisers: [{ id: 'N777', name: 'Nurse Pin', role: 'nurse', department: 'General Medicine' }],
    wards: [
      {
        id: 'WRD-GM', code: 'GM-1', name: null, department: 'General Medicine', ward_type: 'general',
        cleaning_tracked: true, register_complete: true,
        beds: [
          bed({ bed_id: 'BED-4', bed_label: '4', status: 'occupied', status_words: 'Occupied',
            tone: 'occupied', occupied: true, selectable: false }),
          bed({ bed_id: 'BED-5', bed_label: '5' }),
          bed({ bed_id: 'BED-6', bed_label: '6', status: 'needs_cleaning',
            status_words: 'Needs cleaning', tone: 'attention', needs: 'terminal_clean_authorisation',
            terminal_clean: true }),
        ],
      },
      {
        id: 'WRD-ICU', code: 'ICU-1', name: null, department: 'Intensive Care', ward_type: 'icu',
        cleaning_tracked: true, register_complete: true,
        beds: [bed({ bed_id: 'BED-I1', bed_label: 'I1', unit_code: 'ICU-1/I1' })],
      },
    ],
  },
}

const LOCATION = {
  admission_id: 'ADM-7', department: 'General Medicine', ward: 'GM-1', bed: '4', ward_id: 'WRD-GM',
  bed_id: 'BED-4', ward_department: 'General Medicine', linked: true, awaiting_bed: false,
  boarder: false,
}

function refusal(status: number, detail: unknown) {
  return Object.assign(new Error((detail as { message?: string })?.message ?? 'refused'),
    { status, detail })
}

function routeFetch(over: Record<string, unknown> = {}) {
  apiFetch.mockImplementation(async (path: string) => {
    for (const [prefix, answer] of Object.entries(over)) {
      if (path.startsWith(prefix)) {
        if (answer instanceof Error) throw answer
        return typeof answer === 'function' ? (answer as () => unknown)() : answer
      }
    }
    if (path === '/adt/move-options') return OPTIONS
    if (path === '/adt/admissions/ADM-7/location') return LOCATION
    return {}
  })
}

function posts(prefix: string) {
  return apiFetch.mock.calls.filter(([p, init]) => p.startsWith(prefix) && init?.method === 'POST')
}

function body(call: [string, FetchInit?]) {
  return JSON.parse(call[1]?.body ?? '{}')
}

async function renderDrawer(props: Partial<Parameters<typeof MoveDrawer>[0]> = {}) {
  const onDone = vi.fn()
  const onClose = vi.fn()
  await act(async () => {
    render(
      <MoveDrawer
        admissionId="ADM-7"
        patient={{ name: 'Meera Nair', mrn: 'MRN-7' }}
        canTransfer
        canChangeBed
        onDone={onDone}
        onClose={onClose}
        {...props}
      />,
    )
  })
  return { onDone, onClose }
}

const MOVED = {
  replayed: false, admission_id: 'ADM-7', patient_id: 'P7',
  movement: { id: 'MOV-1', movement_type: 'bed_change' },
  current_department: 'General Medicine', current_ward: 'GM-1', current_bed: '5',
  bed_id: 'BED-5', ward_id: 'WRD-GM', linked: true, awaiting_bed: false, boarder: false,
  department_changed: false,
}

beforeEach(() => {
  apiFetch.mockReset()
  role = 'doctor'
  routeFetch()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('MoveDrawer, bed change', () => {
  it('posts the bed, the bed the screen read, a reason and a key to the bed-change route', async () => {
    routeFetch({ '/adt/admissions/ADM-7/bed-change': MOVED })
    const { onDone } = await renderDrawer()
    expect(screen.getByText('Now at').nextSibling).toHaveTextContent('GM-1, bed 4')
    fireEvent.change(screen.getByLabelText('Ward'), { target: { value: 'WRD-GM' } })
    // A bed change always names a bed.
    expect(screen.queryByText('No bed yet')).toBeNull()
    fireEvent.click(screen.getByRole('radio', { name: /Bed 5/ }))
    const send = screen.getByRole('button', { name: 'Record bed change' })
    expect(send).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Reason for the move'), { target: { value: 'Window bed' } })
    expect(send).not.toBeDisabled()
    await act(async () => { fireEvent.click(send) })
    const sent = posts('/adt/admissions/ADM-7/bed-change')
    expect(sent).toHaveLength(1)
    const payload = body(sent[0])
    expect(payload).toMatchObject({ expected_bed_id: 'BED-4', reason: 'Window bed', bed_id: 'BED-5',
      ward_id: 'WRD-GM' })
    expect(payload).not.toHaveProperty('to_department')
    expect(payload).not.toHaveProperty('awaiting_bed')
    expect(sent[0][1]?.headers?.['Idempotency-Key']).toBeTruthy()
    expect(screen.getByText('Move recorded')).toBeInTheDocument()
    expect(screen.getByText('Now at GM-1, bed 5.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    expect(onDone).toHaveBeenCalledTimes(1)
  })

  it("prints the server's refusal and sends the same key again on the retry", async () => {
    let calls = 0
    routeFetch({
      '/adt/admissions/ADM-7/bed-change': () => {
        calls += 1
        if (calls === 1) {
          throw refusal(409, { error: 'state_changed', message: 'Where this patient lies changed.' })
        }
        return MOVED
      },
    })
    await renderDrawer()
    fireEvent.change(screen.getByLabelText('Ward'), { target: { value: 'WRD-GM' } })
    fireEvent.click(screen.getByRole('radio', { name: /Bed 5/ }))
    fireEvent.change(screen.getByLabelText('Reason for the move'), { target: { value: 'Window bed' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Record bed change' })) })
    expect(screen.getByRole('alert')).toHaveTextContent(/changed since this screen opened/)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Record bed change' })) })
    const sent = posts('/adt/admissions/ADM-7/bed-change')
    expect(sent).toHaveLength(2)
    expect(sent[0][1]?.headers?.['Idempotency-Key']).toBe(sent[1][1]?.headers?.['Idempotency-Key'])
  })

  it('clears a signing PIN after it is sent', async () => {
    routeFetch({
      '/adt/admissions/ADM-7/bed-change': () => {
        throw refusal(403, { error: 'terminal_clean_pin_wrong', message: 'The signing PIN is not right.' })
      },
    })
    await renderDrawer()
    fireEvent.change(screen.getByLabelText('Ward'), { target: { value: 'WRD-GM' } })
    fireEvent.click(screen.getByRole('radio', { name: /Bed 6/ }))
    fireEvent.change(screen.getByLabelText('Authorised by'), { target: { value: 'N777' } })
    const pin = screen.getByLabelText('Their signing PIN, typed by them') as HTMLInputElement
    fireEvent.change(pin, { target: { value: '2468' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Ward full' } })
    fireEvent.change(screen.getByLabelText('Reason for the move'), { target: { value: 'Needs a side room' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Record bed change' })) })
    expect(body(posts('/adt/admissions/ADM-7/bed-change')[0]).terminal_clean_authoriser_pin).toBe('2468')
    expect((screen.getByLabelText('Their signing PIN, typed by them') as HTMLInputElement).value).toBe('')
    expect(screen.getByRole('alert')).toHaveTextContent('The signing PIN is not right.')
  })
})

describe('MoveDrawer, transfer', () => {
  it('offers only covered departments, says the chart is handed over, and posts the transfer', async () => {
    routeFetch({
      '/adt/transfers': {
        ...MOVED, movement: { id: 'MOV-2', movement_type: 'transfer' }, current_department: 'Intensive Care',
        current_ward: 'ICU-1', current_bed: 'I1', department_changed: true,
      },
    })
    await renderDrawer({ initialMode: 'transfer' })
    const dept = screen.getByLabelText('Receiving department')
    expect(within(dept).getAllByRole('option').map((o) => o.textContent))
      .toEqual(['Select...', 'General Medicine', 'Intensive Care'])
    fireEvent.change(dept, { target: { value: 'Intensive Care' } })
    expect(screen.getByTestId('move-handover')).toHaveTextContent(
      /staff in Intensive Care can open this patient's record and staff in General Medicine can no longer see them/)
    fireEvent.change(screen.getByLabelText('Ward'), { target: { value: 'WRD-ICU' } })
    fireEvent.click(screen.getByRole('radio', { name: /Bed I1/ }))
    fireEvent.change(screen.getByLabelText('Reason for the move'), { target: { value: 'Needs ventilation' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Record transfer' })) })
    const sent = posts('/adt/transfers')
    expect(sent).toHaveLength(1)
    expect(body(sent[0])).toMatchObject({
      admission_id: 'ADM-7', to_department: 'Intensive Care', bed_id: 'BED-I1', ward_id: 'WRD-ICU',
      expected_bed_id: 'BED-4', reason: 'Needs ventilation',
    })
    expect(screen.getByText('Meera Nair is now treated by Intensive Care.')).toBeInTheDocument()
    expect(screen.getByText(/Staff in General Medicine can no longer open this chart/)).toBeInTheDocument()
  })

  it('keeping the bed under another department needs the boarder words confirmed', async () => {
    routeFetch({ '/adt/transfers': { ...MOVED, department_changed: true, boarder: true } })
    await renderDrawer({ initialMode: 'transfer' })
    fireEvent.change(screen.getByLabelText('Receiving department'), { target: { value: 'Intensive Care' } })
    fireEvent.click(screen.getByRole('radio', { name: /Stays in bed 4 for now/ }))
    expect(screen.getByText(/Nurses on GM-1 \(General Medicine\) will\s+not be able to open this chart/))
      .toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Reason for the move'), { target: { value: 'Accepted, no ICU bed' } })
    const send = screen.getByRole('button', { name: 'Record transfer' })
    expect(send).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: /Keep the patient here as a boarder/ }))
    expect(send).not.toBeDisabled()
    await act(async () => { fireEvent.click(send) })
    const payload = body(posts('/adt/transfers')[0])
    expect(payload).toMatchObject({ keep_location: true, boarder_acknowledged: true,
      to_department: 'Intensive Care', expected_bed_id: 'BED-4' })
    expect(payload).not.toHaveProperty('bed_id')
    expect(payload).not.toHaveProperty('ward_id')
  })

  it('a ward of another department in the picker is a boarder placement, confirmed first', async () => {
    await renderDrawer({ initialMode: 'transfer' })
    fireEvent.change(screen.getByLabelText('Receiving department'), { target: { value: 'Intensive Care' } })
    fireEvent.change(screen.getByLabelText('Ward'), { target: { value: 'WRD-GM' } })
    expect(screen.getByText(/The Intensive Care team keeps it/)).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /Move as a boarder/ })).toBeInTheDocument()
  })

  it('keeping the bed, a refusal is printed and a bed out of service asks for its reason', async () => {
    let calls = 0
    const words = 'Bed 4 on GM-1 is recorded out of service by housekeeping. Type the reason the '
      + 'patient is placed there anyway; it is recorded.'
    routeFetch({
      '/adt/transfers': () => {
        calls += 1
        if (calls === 1) {
          throw refusal(409, { error: 'out_of_service_reason_required', message: words, bed_id: 'BED-4' })
        }
        return { ...MOVED, department_changed: true, boarder: true, current_department: 'Intensive Care',
          current_bed: '4' }
      },
    })
    await renderDrawer({ initialMode: 'transfer' })
    fireEvent.change(screen.getByLabelText('Receiving department'), { target: { value: 'Intensive Care' } })
    fireEvent.click(screen.getByRole('radio', { name: /Stays in bed 4 for now/ }))
    fireEvent.click(screen.getByRole('checkbox', { name: /Keep the patient here as a boarder/ }))
    fireEvent.change(screen.getByLabelText('Reason for the move'), { target: { value: 'Accepted, no ICU bed' } })
    // Nothing foresaw it: no reason is asked before the first press.
    expect(screen.queryByLabelText(/is recorded out of service by housekeeping/)).toBeNull()
    const send = screen.getByRole('button', { name: 'Record transfer' })
    await act(async () => { fireEvent.click(send) })

    // The server's words, in an alert, with no picker on screen to hold them.
    expect(screen.getByRole('alert')).toHaveTextContent(words)
    expect(screen.queryByText('Move recorded')).toBeNull()
    const why = screen.getByLabelText(/Bed 4 is recorded out of service by\s+housekeeping/)
    expect(send).toBeDisabled()
    expect(screen.getByTestId('move-missing')).toHaveTextContent(
      'Type the reason the patient stays in a bed out of service.')
    fireEvent.change(why, { target: { value: 'Rail taped, patient stable' } })
    expect(send).not.toBeDisabled()
    await act(async () => { fireEvent.click(send) })

    const sent = posts('/adt/transfers')
    expect(sent).toHaveLength(2)
    expect(body(sent[0])).not.toHaveProperty('out_of_service_reason')
    expect(body(sent[1])).toMatchObject({ keep_location: true, boarder_acknowledged: true,
      out_of_service_reason: 'Rail taped, patient stable', expected_bed_id: 'BED-4' })
    expect(sent[0][1]?.headers?.['Idempotency-Key']).toBe(sent[1][1]?.headers?.['Idempotency-Key'])
    expect(screen.getByText('Move recorded')).toBeInTheDocument()
  })

  it('keeping an unlinked place the server names a boarder for opens the confirmation', async () => {
    let calls = 0
    const words = 'Nurses on SUR-1 (Surgery) will not be able to open this chart. '
      + 'The Intensive Care team keeps it.'
    routeFetch({
      '/adt/admissions/ADM-7/location': {
        ...LOCATION, ward: 'sur-1', bed: '9', ward_id: null, bed_id: null, ward_department: null,
        linked: false,
      },
      '/adt/transfers': () => {
        calls += 1
        if (calls === 1) {
          throw refusal(409, { error: 'boarder_acknowledgement_required', message: words,
            ward_department: 'Surgery', treating_department: 'Intensive Care' })
        }
        return { ...MOVED, department_changed: true, boarder: true, current_department: 'Intensive Care' }
      },
    })
    await renderDrawer({ initialMode: 'transfer' })
    fireEvent.change(screen.getByLabelText('Receiving department'), { target: { value: 'Intensive Care' } })
    fireEvent.click(screen.getByRole('radio', { name: /Stays in bed 9 for now/ }))
    fireEvent.change(screen.getByLabelText('Reason for the move'), { target: { value: 'Accepted' } })
    expect(screen.queryByRole('checkbox', { name: /Keep the patient here as a boarder/ })).toBeNull()
    const send = screen.getByRole('button', { name: 'Record transfer' })
    await act(async () => { fireEvent.click(send) })

    expect(screen.getByRole('alert')).toHaveTextContent(words)
    expect(screen.getByText(/Nurses on sur-1 \(Surgery\) will\s+not be able to open this chart/))
      .toBeInTheDocument()
    expect(send).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: /Keep the patient here as a boarder/ }))
    expect(send).not.toBeDisabled()
    await act(async () => { fireEvent.click(send) })
    const sent = posts('/adt/transfers')
    expect(sent).toHaveLength(2)
    expect(body(sent[0])).not.toHaveProperty('boarder_acknowledged')
    expect(body(sent[1])).toMatchObject({ keep_location: true, boarder_acknowledged: true,
      expected_bed_id: null, to_department: 'Intensive Care' })
    expect(screen.getByText('Move recorded')).toBeInTheDocument()
  })

  it('the place read asks before the first press for what keeping the place needs', async () => {
    routeFetch({
      '/adt/admissions/ADM-7/location': {
        ...LOCATION, ward: 'sur-1', bed: '9', ward_id: null, bed_id: null, ward_department: null,
        linked: false,
        kept_place: { ward: 'SUR-1', ward_department: 'Surgery', bed: '9', out_of_service: true },
      },
      '/adt/transfers': { ...MOVED, department_changed: true, boarder: true },
    })
    await renderDrawer({ initialMode: 'transfer' })
    fireEvent.change(screen.getByLabelText('Receiving department'), { target: { value: 'Intensive Care' } })
    fireEvent.click(screen.getByRole('radio', { name: /Stays in bed 9 for now/ }))
    expect(screen.getByText(/Nurses on SUR-1 \(Surgery\) will\s+not be able to open this chart/))
      .toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Reason for the move'), { target: { value: 'Accepted' } })
    const send = screen.getByRole('button', { name: 'Record transfer' })
    expect(send).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: /Keep the patient here as a boarder/ }))
    expect(send).toBeDisabled()
    fireEvent.change(screen.getByLabelText(/Bed 9 is recorded out of service by\s+housekeeping/),
      { target: { value: 'Only monitored bed' } })
    expect(send).not.toBeDisabled()
    await act(async () => { fireEvent.click(send) })
    const sent = posts('/adt/transfers')
    expect(sent).toHaveLength(1)
    expect(body(sent[0])).toMatchObject({ keep_location: true, boarder_acknowledged: true,
      out_of_service_reason: 'Only monitored bed' })
  })

  it('keeping the bed, any other refusal about the place is printed in its words', async () => {
    routeFetch({
      '/adt/transfers': refusal(409, { error: 'bed_occupied',
        message: 'Bed 4 on GM-1 already holds a patient. Choose another bed, or add an extra bed with its own label.' }),
    })
    await renderDrawer({ initialMode: 'transfer' })
    fireEvent.change(screen.getByLabelText('Receiving department'), { target: { value: 'Intensive Care' } })
    fireEvent.click(screen.getByRole('radio', { name: /Stays in bed 4 for now/ }))
    fireEvent.click(screen.getByRole('checkbox', { name: /Keep the patient here as a boarder/ }))
    fireEvent.change(screen.getByLabelText('Reason for the move'), { target: { value: 'Accepted' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Record transfer' })) })
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(screen.getByRole('alert')).toHaveTextContent('Bed 4 on GM-1 already holds a patient.')
    expect(screen.queryByText('Move recorded')).toBeNull()
  })

  it('No bed yet on the receiving ward is sent as awaiting a bed', async () => {
    routeFetch({ '/adt/transfers': { ...MOVED, awaiting_bed: true, current_bed: null } })
    await renderDrawer({ initialMode: 'transfer' })
    fireEvent.change(screen.getByLabelText('Receiving department'), { target: { value: 'Intensive Care' } })
    fireEvent.change(screen.getByLabelText('Ward'), { target: { value: 'WRD-ICU' } })
    fireEvent.click(screen.getByRole('radio', { name: /No bed yet/ }))
    expect(screen.getByText('Moved to the ward, awaiting a bed')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Reason for the move'), { target: { value: 'Corridor bed' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Record transfer' })) })
    expect(body(posts('/adt/transfers')[0])).toMatchObject({ awaiting_bed: true, ward_id: 'WRD-ICU' })
  })
})

describe('MoveDrawer, who may do what, and failed reads', () => {
  it('a role that may only change beds is offered no transfer', async () => {
    await renderDrawer({ canTransfer: false })
    expect(screen.queryByText('Transfer to another department')).toBeNull()
    expect(screen.queryByLabelText('Receiving department')).toBeNull()
    expect(screen.getByRole('button', { name: 'Record bed change' })).toBeInTheDocument()
  })

  it('a read that failed says so and can be tried again', async () => {
    routeFetch({ '/adt/admissions/ADM-7/location': refusal(404, { message: 'Admission not found' }) })
    await renderDrawer()
    expect(screen.getByRole('alert')).toHaveTextContent('This patient is not on your list')
    routeFetch()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Try again' })) })
    expect(screen.getByRole('button', { name: 'Record bed change' })).toBeInTheDocument()
  })
})

describe('Move patient tile action', () => {
  const WARD = { id: 'WRD-GM', code: 'GM-1', name: null, department: 'General Medicine', ward_type: null,
                 is_active: true, cleaning_tracked: true, register_complete: true, counts: {}, tiles: [] } as BoardWard

  function tile(over: Partial<BedTile> = {}): BedTile {
    return {
      bed_id: 'BED-4', unit_code: 'GM-1/4', bed_label: '4', ward_id: 'WRD-GM', room_id: null, room: null,
      room_name: null, bed_class: null, isolation: false, temporary: false, status: 'occupied',
      status_words: 'Occupied', tone: 'occupied', markers: [], free: false, housekeeping_state: 'occupied',
      since: null, minutes_in_state: null, occupant_hidden: false,
      occupant: { admission_id: 'ADM-7', patient_id: 'P7', name: 'Meera Nair', mrn: 'MRN-7',
                  department: 'General Medicine', boarder: false, boarder_from: null,
                  presence_status: null, linked: true },
      ...over,
    }
  }

  it('is installed as Move patient, on occupied tiles whose occupant the caller may see', () => {
    const entry = TILE_ACTIONS.find((a) => a.id === 'move')
    expect(entry?.label).toBe('Move patient')
    expect(entry?.needsOccupant).toBe(true)
    const entries = discoverTileActions({ './MoveAction.tsx': moveActionModule })
    const any = () => true
    expect(actionsForTile(entries, tile(), any)).toHaveLength(1)
    expect(actionsForTile(entries, tile({ status: 'boarder' }), any)).toHaveLength(1)
    expect(actionsForTile(entries, tile({ occupant: null, occupant_hidden: true }), any)).toHaveLength(0)
    expect(actionsForTile(entries, tile({ status: 'available', occupant: null }), any)).toHaveLength(0)
  })

  it('offers the ward clerk a bed change only', async () => {
    role = 'ward_clerk'
    await act(async () => {
      render(<MoveAction tile={tile()} ward={WARD} onDone={vi.fn()} onClose={vi.fn()} />)
    })
    expect(screen.getByText('Move Meera Nair')).toBeInTheDocument()
    expect(screen.queryByText('Transfer to another department')).toBeNull()
    expect(screen.getByRole('button', { name: 'Record bed change' })).toBeInTheDocument()
  })

  it('offers a doctor both moves', async () => {
    await act(async () => {
      render(<MoveAction tile={tile()} ward={WARD} onDone={vi.fn()} onClose={vi.fn()} />)
    })
    expect(screen.getByText('Transfer to another department')).toBeInTheDocument()
    expect(screen.getByText('Change bed, same department')).toBeInTheDocument()
  })
})
