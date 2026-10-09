/**
 * Leave and absconded (adt-10) and bed reservations (adt-12) on screen.
 *
 * What they must never get wrong:
 *
 * 1. 'NOT ON WARD SINCE' IS LOCAL TIME, through src/lib/datetime, and only
 *    when the row says the patient is off the ward. A row with no presence
 *    (the module off) draws nothing.
 * 2. THE PRESENCE SCREEN offers only what can start from where the patient is
 *    (on the ward: a leave or absconding; on leave: back, or did not come
 *    back; absconded: back on the ward), holds the button until a leave or an
 *    absconding has its reason, posts the right body to the right route with
 *    an Idempotency-Key, prints the server's refusal in its words, and a
 *    retry sends the same key. It says that due times are not paused.
 * 3. THE TILE ACTIONS are found by the board; a ward clerk is told who
 *    records leave; a reserved bed's tile says when it expires and never names
 *    a patient outside the caller's department.
 * 4. RESERVE AND RELEASE post the right bodies; a transfer cannot be sent
 *    without its patient; placing the patient a bed is held for gives an
 *    admission awaiting a bed that bed, and asks for the not-clean reason
 *    when the server does, retrying with the same key.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

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

import PresenceDrawer from './PresenceDrawer'
import NotOnWardChip from './NotOnWardChip'
import BoardTileNotes from './BoardTileNotes'
import PresenceAction from './actions/PresenceAction'
import ReserveAction from './actions/ReserveAction'
import ReservationAction from './actions/ReservationAction'
import { TILE_ACTIONS, actionsForTile } from './actions'
import { notOnWardWords } from '../../api/adt/presence'
import { formatClinicalDateTime, formatClinicalTime } from '../../lib/datetime'
import type { BedTile, BoardWard } from '../../api/adt/board'

function refusal(status: number, detail: unknown) {
  return Object.assign(new Error((detail as { message?: string })?.message ?? 'refused'),
    { status, detail })
}

function routeFetch(over: Record<string, unknown> = {}) {
  apiFetch.mockImplementation(async (path: string) => {
    for (const [prefix, answer] of Object.entries(over)) {
      if (path.startsWith(prefix)) {
        const value = typeof answer === 'function' ? (answer as () => unknown)() : answer
        if (value instanceof Error) throw value
        return value
      }
    }
    if (path === '/adt/meta') {
      return { module: 'adt', state: 'on', settings: { backtime_hours: 72, reservation_minutes: 120,
        reservation_elective_minutes: 1440 } }
    }
    return {}
  })
}

function posts(prefix: string) {
  return apiFetch.mock.calls.filter(([p, init]) => p.startsWith(prefix) && init?.method === 'POST')
}

const WARD: BoardWard = {
  id: 'W1', code: 'GM-1', name: 'General 1', department: 'General Medicine', ward_type: null,
  is_active: true, cleaning_tracked: false, register_complete: false, counts: {}, tiles: [],
}

function tile(over: Partial<BedTile> & Record<string, unknown> = {}): BedTile {
  return {
    bed_id: 'BED-7', unit_code: 'GM-1/7', bed_label: '7', ward_id: 'W1', room_id: null, room: null,
    room_name: null, bed_class: null, isolation: false, temporary: false, status: 'occupied',
    status_words: 'Occupied', tone: 'occupied', markers: [], free: false, housekeeping_state: null,
    since: null, minutes_in_state: null,
    occupant: {
      admission_id: 'ADM-1', patient_id: 'P1', name: 'Asha Menon', mrn: 'MRN-1',
      department: 'General Medicine', boarder: false, boarder_from: null, presence_status: null,
      linked: true,
    },
    occupant_hidden: false,
    ...over,
  } as BedTile
}

const PRESENCE_RESULT = {
  replayed: false, admission_id: 'ADM-1', patient_id: 'P1', presence_status: 'on_leave',
  presence_words: 'On leave', presence_since: '2026-10-09T08:30:00+00:00',
  movement: { id: 'MOV-1', movement_type: 'leave_start', moved_at: '2026-10-09T08:30:00+00:00' },
}

beforeEach(() => {
  apiFetch.mockReset()
  role = 'doctor'
  routeFetch()
})

// ═════════════════════════════════════════════════════════════════════════════
//  1. Not on ward since
// ═════════════════════════════════════════════════════════════════════════════

describe('the not-on-ward chip', () => {
  it('prints the time the patient left in local time, today as a time', () => {
    const since = new Date(Date.now() - 30 * 60_000).toISOString()
    render(<NotOnWardChip row={{ presence_status: 'on_leave', presence_since: since }} />)
    const chip = screen.getByTestId('not-on-ward')
    expect(chip).toHaveTextContent(`Not on ward since ${formatClinicalTime(since)} (on leave)`)
    // Never the stored UTC spelling.
    expect(chip.textContent).not.toContain('T')
    expect(chip.className).toContain('adt-not-on-ward--leave')
  })

  it('prints the date as well when the patient left on another day', () => {
    const since = '2026-10-01T08:30:00+00:00'
    const words = notOnWardWords({ presence_status: 'absconded', presence_since: since },
      new Date('2026-10-09T12:00:00+00:00'))
    expect(words).toBe(`Not on ward since ${formatClinicalDateTime(since)} (absconded)`)
  })

  it('draws nothing for a patient on the ward, or a row from a site with the module off', () => {
    const { container } = render(
      <>
        <NotOnWardChip row={{ presence_status: null, presence_since: null }} />
        <NotOnWardChip row={{ id: 'P1', name: 'Asha Menon' }} />
        <NotOnWardChip row={null} />
      </>,
    )
    expect(container.querySelector('.adt-not-on-ward')).toBeNull()
  })

  it('marks an absconded patient with the warning style', () => {
    render(<NotOnWardChip row={{ presence_status: 'absconded', presence_since: null }} />)
    expect(screen.getByTestId('not-on-ward')).toHaveTextContent('Not on ward (absconded)')
    expect(screen.getByTestId('not-on-ward').className).toContain('adt-not-on-ward--absconded')
  })
})

// ═════════════════════════════════════════════════════════════════════════════
//  2. The presence screen
// ═════════════════════════════════════════════════════════════════════════════

async function renderDrawer(props: Partial<Parameters<typeof PresenceDrawer>[0]> = {}) {
  const onDone = vi.fn()
  const onClose = vi.fn()
  await act(async () => {
    render(
      <PresenceDrawer admissionId="ADM-1" patient={{ name: 'Asha Menon', mrn: 'MRN-1', ward: 'GM-1', bed: '7' }}
        presence={null} onDone={onDone} onClose={onClose} {...props} />,
    )
  })
  return { onDone, onClose }
}

const recordButton = () => screen.getByRole('button', { name: /^Record/ })

describe('the presence screen', () => {
  it('offers a leave or absconding to a patient on the ward, and holds the button for the reason', async () => {
    await renderDrawer()
    const group = screen.getByRole('group', { name: 'What to record' })
    expect(within(group).getAllByRole('radio').map((r) => (r as HTMLInputElement).value))
      .toEqual(['leave', 'absconded'])
    expect(recordButton()).toBeDisabled()
    fireEvent.click(screen.getByRole('radio', { name: 'Start leave' }))
    expect(recordButton()).toBeDisabled()
    expect(screen.getByText(/are not paused for a patient off/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Home for the festival' } })
    expect(recordButton()).toBeEnabled()
  })

  it('posts the leave with its reason, time and key, then says what was recorded', async () => {
    routeFetch({ '/adt/admissions/ADM-1/leave': PRESENCE_RESULT })
    const { onDone } = await renderDrawer()
    fireEvent.click(screen.getByRole('radio', { name: 'Start leave' }))
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: ' Home for the festival ' } })
    await act(async () => { fireEvent.click(recordButton()) })
    const [call] = posts('/adt/admissions/')
    expect(call[0]).toBe('/adt/admissions/ADM-1/leave')
    const body = JSON.parse(call[1]?.body ?? '{}')
    expect(body.reason).toBe('Home for the festival')
    expect(Number.isNaN(Date.parse(body.occurred_at))).toBe(false)
    expect(body).not.toHaveProperty('expected_return_at')
    expect(call[1]?.headers?.['Idempotency-Key']).toBeTruthy()
    const result = await screen.findByTestId('presence-result')
    expect(result).toHaveTextContent('On leave')
    expect(result).toHaveTextContent("The bed stays this patient's.")
    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    expect(onDone).toHaveBeenCalledWith(PRESENCE_RESULT)
  })

  it("prints the server's refusal in its words, and a retry sends the same key", async () => {
    routeFetch({
      '/adt/admissions/ADM-1/leave': refusal(409, {
        error: 'already_on_leave',
        message: 'This patient is already recorded on leave. Record the return from leave before starting another.',
      }),
    })
    await renderDrawer()
    fireEvent.click(screen.getByRole('radio', { name: 'Start leave' }))
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Day leave' } })
    await act(async () => { fireEvent.click(recordButton()) })
    expect(await screen.findByRole('alert')).toHaveTextContent('already recorded on leave')
    await act(async () => { fireEvent.click(recordButton()) })
    const [first, second] = posts('/adt/admissions/')
    expect(second[1]?.headers?.['Idempotency-Key']).toBe(first[1]?.headers?.['Idempotency-Key'])
  })

  it('a ward clerk is told plainly that only a doctor or the administrator records leave', async () => {
    routeFetch({ '/adt/admissions/ADM-1/absconded': refusal(403, { error: 'permission_denied' }) })
    await renderDrawer()
    fireEvent.click(screen.getByRole('radio', { name: 'Absconded' }))
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Missing at the round' } })
    await act(async () => { fireEvent.click(recordButton()) })
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Only a doctor or the administrator can record leave and absconding. Nothing was recorded.')
  })

  it('offers back from leave, or did not come back, to a patient on leave', async () => {
    routeFetch({ '/adt/admissions/ADM-1/leave-end': { ...PRESENCE_RESULT, presence_status: null } })
    await renderDrawer({ presence: 'on_leave', presenceSince: new Date().toISOString() })
    expect(screen.getByTestId('presence-now')).toHaveTextContent('On leave, not on the ward since')
    const group = screen.getByRole('group', { name: 'What to record' })
    expect(within(group).getAllByRole('radio').map((r) => (r as HTMLInputElement).value))
      .toEqual(['leave-end', 'absconded'])
    expect(screen.getByRole('radio', { name: 'Did not come back from leave (absconded)' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: 'Back from leave' }))
    // No reason is needed for a return.
    expect(recordButton()).toBeEnabled()
    await act(async () => { fireEvent.click(recordButton()) })
    expect(posts('/adt/admissions/')[0][0]).toBe('/adt/admissions/ADM-1/leave-end')
    expect(await screen.findByTestId('presence-result')).toHaveTextContent('Back on the ward')
  })

  it('offers only the return to an absconded patient, already chosen', async () => {
    await renderDrawer({ presence: 'absconded' })
    const radios = within(screen.getByRole('group', { name: 'What to record' })).getAllByRole('radio')
    expect(radios.map((r) => (r as HTMLInputElement).value)).toEqual(['returned'])
    expect(radios[0]).toBeChecked()
    expect(recordButton()).toBeEnabled()
  })

  it('refuses a time beyond the back-time window before sending', async () => {
    await renderDrawer()
    fireEvent.click(screen.getByRole('radio', { name: 'Start leave' }))
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Day leave' } })
    fireEvent.change(screen.getByLabelText('When the patient left the ward'), { target: { value: '2020-01-01T08:00' } })
    expect(screen.getByText(/up to 72 hours back/)).toBeInTheDocument()
    expect(recordButton()).toBeDisabled()
  })
})

// ═════════════════════════════════════════════════════════════════════════════
//  3. Tile actions and tile notes
// ═════════════════════════════════════════════════════════════════════════════

describe('the bed board', () => {
  it('finds the presence and reservation actions, each on its own tiles', () => {
    const ids = (t: BedTile) => actionsForTile(TILE_ACTIONS, t, () => true).map((e) => e.id)
    expect(ids(tile())).toContain('presence')
    expect(ids(tile({ status: 'occupied_on_leave' }))).toContain('presence')
    expect(ids(tile({ status: 'occupied', occupant: null, occupant_hidden: true }))).not.toContain('presence')
    expect(ids(tile({ status: 'available', occupant: null }))).toContain('reserve')
    expect(ids(tile({ status: 'available', occupant: null }))).not.toContain('reservation')
    expect(ids(tile({ status: 'reserved', occupant: null }))).toContain('reservation')
    expect(ids(tile({ status: 'reserved', occupant: null }))).not.toContain('reserve')
  })

  it('a ward clerk opening Leave or absconded is told who records it', () => {
    role = 'ward_clerk'
    render(<PresenceAction tile={tile()} ward={WARD} onDone={vi.fn()} onClose={vi.fn()} />)
    expect(screen.getByText('Leave and absconding are recorded by a doctor or the administrator.')).toBeInTheDocument()
    expect(screen.queryByTestId('presence')).toBeNull()
  })

  it('a doctor opening it gets the screen with the patient read back', async () => {
    await act(async () => {
      render(<PresenceAction tile={tile({ status: 'occupied_on_leave', occupant: {
        ...tile().occupant!, presence_status: 'on_leave', presence_since: '2026-10-09T08:30:00+00:00',
      } as BedTile['occupant'] })} ward={WARD} onDone={vi.fn()} onClose={vi.fn()} />)
    })
    expect(screen.getByTestId('presence')).toHaveTextContent('Asha Menon')
    expect(screen.getByTestId('presence-now')).toHaveTextContent('On leave')
  })

  it('a tile shows when its patient left the ward', () => {
    const since = new Date(Date.now() - 60 * 60_000).toISOString()
    render(<BoardTileNotes tile={tile({ status: 'occupied_on_leave', occupant: {
      ...tile().occupant!, presence_status: 'on_leave', presence_since: since,
    } as BedTile['occupant'] })} />)
    expect(screen.getByTestId('tile-not-on-ward')).toHaveTextContent(`Not on ward since ${formatClinicalTime(since)}`)
  })

  it('a reserved tile says when it expires, and never names a patient outside scope', () => {
    const expires = new Date(Date.now() + 90 * 60_000).toISOString()
    render(<BoardTileNotes tile={tile({
      status: 'reserved', status_words: 'Reserved for transfer', occupant: null,
      reservation: { id: 'RES-1', purpose: 'transfer', purpose_words: 'Reserved for transfer',
        reserved_at: new Date().toISOString(), expires_at: expires, patient: null, patient_hidden: true },
    })} />)
    expect(screen.getByTestId('tile-reservation-expiry')).toHaveTextContent(`Expires ${formatClinicalTime(expires)}`)
    expect(screen.getByText('Held for a patient outside your department')).toBeInTheDocument()
  })

  it('a reserved tile inside scope says who it is held for', () => {
    render(<BoardTileNotes tile={tile({
      status: 'reserved', occupant: null,
      reservation: { id: 'RES-1', purpose: 'admission', purpose_words: 'Reserved for an admission',
        reserved_at: null, expires_at: null, patient_hidden: false,
        patient: { patient_id: 'P9', admission_id: 'ADM-9', name: 'Ravi Kumar', mrn: 'MRN-9' } },
    })} />)
    expect(screen.getByText('For Ravi Kumar, MRN-9')).toBeInTheDocument()
  })
})

// ═════════════════════════════════════════════════════════════════════════════
//  4. Reserve, release and place
// ═════════════════════════════════════════════════════════════════════════════

const BOARD = {
  generated_at: '2026-10-09T08:00:00+00:00', department: 'General Medicine', ward_id: null,
  ward_choices: [], status_words: {},
  wards: [{ ...WARD, tiles: [tile()] }],
}
const AWAITING = {
  ward_id: null, counts: { awaiting: 1, unlinked: 0 }, unlinked: [],
  awaiting: [{ admission_id: 'ADM-2', patient_id: 'P2', name: 'Kiran Das', mrn: 'MRN-2',
    department: 'General Medicine', ward: null, bed: null, ward_id: null, bed_id: null,
    master_ward_id: null, master_ward_code: null, master_bed_id: null, master_bed_label: null,
    master_unit_code: null, reason: 'awaiting_bed', reason_words: 'No bed recorded yet', since: null }],
}

describe('reserving a bed', () => {
  const freeTile = () => tile({ bed_id: 'BED-9', bed_label: '9', status: 'available', occupant: null, free: true })

  async function renderReserve() {
    const onDone = vi.fn()
    await act(async () => {
      render(<ReserveAction tile={freeTile()} ward={WARD} onDone={onDone} onClose={vi.fn()} />)
    })
    return { onDone }
  }

  it('a transfer cannot be sent without its patient, and posts the purpose and admission', async () => {
    routeFetch({
      '/adt/board': BOARD, '/adt/awaiting-bed': AWAITING,
      '/adt/reservations': { replayed: false, reservation: {
        id: 'RES-1', purpose: 'transfer', purpose_words: 'Reserved for transfer',
        expires_at: new Date(Date.now() + 7_200_000).toISOString() } },
    })
    await renderReserve()
    await waitFor(() => expect(screen.getByRole('option', { name: /Kiran Das/ })).toBeInTheDocument())
    expect(screen.getByRole('option', { name: /Asha Menon, MRN-1 \(GM-1 bed 7\)/ })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: 'A transfer from another ward' }))
    expect(screen.getByText('Held for 2 hours, the site\'s setting.')).toBeInTheDocument()
    const reserve = screen.getByRole('button', { name: 'Reserve bed' })
    expect(reserve).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Patient'), { target: { value: 'ADM-1' } })
    expect(reserve).toBeEnabled()
    await act(async () => { fireEvent.click(reserve) })
    const [call] = posts('/adt/reservations')
    expect(JSON.parse(call[1]?.body ?? '{}')).toEqual({ bed_id: 'BED-9', purpose: 'transfer', admission_id: 'ADM-1' })
    expect(call[1]?.headers?.['Idempotency-Key']).toBeTruthy()
    expect(await screen.findByTestId('reserve-result')).toHaveTextContent('Reserved for transfer: bed 9 on GM-1, expires')
  })

  it('a planned admission can be held with no patient named, for 24 hours', async () => {
    routeFetch({ '/adt/board': BOARD, '/adt/awaiting-bed': AWAITING,
      '/adt/reservations': { replayed: false, reservation: { id: 'RES-2', purpose_words: 'Reserved for a planned admission', expires_at: null } } })
    await renderReserve()
    fireEvent.click(screen.getByRole('radio', { name: 'A planned admission' }))
    expect(screen.getByText('Held for 24 hours, the site\'s setting.')).toBeInTheDocument()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Reserve bed' })) })
    expect(JSON.parse(posts('/adt/reservations')[0][1]?.body ?? '{}')).toEqual({ bed_id: 'BED-9', purpose: 'elective' })
  })

  it('a nurse is told who reserves beds', () => {
    role = 'nurse'
    render(<ReserveAction tile={freeTile()} ward={WARD} onDone={vi.fn()} onClose={vi.fn()} />)
    expect(screen.getByText('Beds are reserved by a ward clerk, a doctor or the administrator.')).toBeInTheDocument()
  })
})

describe('a reserved bed', () => {
  const heldTile = (patient: Record<string, unknown> | null = {
    patient_id: 'P2', admission_id: 'ADM-2', name: 'Kiran Das', mrn: 'MRN-2' }) => tile({
    bed_id: 'BED-9', bed_label: '9', status: 'reserved', status_words: 'Reserved for an admission',
    occupant: null,
    reservation: { id: 'RES-1', purpose: 'admission', purpose_words: 'Reserved for an admission',
      reserved_at: null, expires_at: new Date(Date.now() + 3_600_000).toISOString(),
      patient, patient_hidden: !patient },
  })

  it('releases with a reason', async () => {
    role = 'ward_clerk'
    routeFetch({ '/adt/reservations/RES-1/release': { reservation: { id: 'RES-1', status: 'released' } } })
    const onDone = vi.fn()
    render(<ReservationAction tile={heldTile()} ward={WARD} onDone={onDone} onClose={vi.fn()} />)
    const release = screen.getByRole('button', { name: 'Release reservation' })
    expect(release).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Why release it'), { target: { value: 'Patient went home' } })
    await act(async () => { fireEvent.click(release) })
    expect(JSON.parse(posts('/adt/reservations/RES-1/release')[0][1]?.body ?? '{}')).toEqual({ reason: 'Patient went home' })
    expect(await screen.findByTestId('reservation-released')).toBeInTheDocument()
  })

  it('places the patient it is held for, asking for the not-clean reason when the server does', async () => {
    let tries = 0
    routeFetch({
      '/adt/admissions/ADM-2/location': { admission_id: 'ADM-2', bed_id: null, bed: null, awaiting_bed: true },
      '/adt/admissions/ADM-2/bed': () => {
        tries += 1
        return tries === 1
          ? refusal(409, { error: 'bed_not_clean_reason_required',
              message: 'Bed 9 on GM-1 has not been recorded clean (needs cleaning). Type the reason the patient is placed there before it is cleaned; it is recorded.' })
          : { replayed: false, movement: { id: 'MOV-9' } }
      },
    })
    render(<ReservationAction tile={heldTile()} ward={WARD} onDone={vi.fn()} onClose={vi.fn()} />)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Place in this bed' })) })
    expect(await screen.findByRole('alert')).toHaveTextContent('has not been recorded clean')
    const place = screen.getByRole('button', { name: 'Place in this bed' })
    expect(place).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Why the bed is used before it is recorded clean'),
      { target: { value: 'Patient arriving from the ER now' } })
    await act(async () => { fireEvent.click(place) })
    const [first, second] = posts('/adt/admissions/ADM-2/bed')
    expect(JSON.parse(first[1]?.body ?? '{}')).toEqual({ bed_id: 'BED-9' })
    expect(JSON.parse(second[1]?.body ?? '{}')).toEqual({ bed_id: 'BED-9', not_clean_reason: 'Patient arriving from the ER now' })
    expect(second[1]?.headers?.['Idempotency-Key']).toBe(first[1]?.headers?.['Idempotency-Key'])
    expect(await screen.findByTestId('reservation-placed')).toHaveTextContent('The reservation is fulfilled.')
  })

  it('moves a patient already in a bed with the bed they lie in as the expected bed', async () => {
    routeFetch({
      '/adt/admissions/ADM-2/location': { admission_id: 'ADM-2', bed_id: 'BED-3', bed: '3', awaiting_bed: false },
      '/adt/admissions/ADM-2/bed-change': { replayed: false, movement: { id: 'MOV-10' } },
    })
    render(<ReservationAction tile={heldTile()} ward={WARD} onDone={vi.fn()} onClose={vi.fn()} />)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Place in this bed' })) })
    const body = JSON.parse(posts('/adt/admissions/ADM-2/bed-change')[0][1]?.body ?? '{}')
    expect(body.bed_id).toBe('BED-9')
    expect(body.expected_bed_id).toBe('BED-3')
    expect(body.reason).toBeTruthy()
  })

  it('offers no placing and names nobody when the patient is outside scope', () => {
    render(<ReservationAction tile={heldTile(null)} ward={WARD} onDone={vi.fn()} onClose={vi.fn()} />)
    expect(screen.getByText('A patient outside your department')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Place in this bed' })).toBeNull()
  })
})
