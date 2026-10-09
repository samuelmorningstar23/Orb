/**
 * The bed board's 'Swap beds' action (adt-6b).
 *
 * What it must never get wrong:
 *
 * 1. IT IS INSTALLED on occupied tiles whose patient the caller may see, and
 *    lists as partners only other patients the caller may see in beds of bed
 *    setup (a typed bed is linked first).
 * 2. IT POSTS BOTH ADMISSIONS AND BOTH BEDS THE BOARD SHOWED to
 *    /api/adt/bed-swaps, with one Idempotency-Key for the screen, and the
 *    retry after a refusal carries what the server asked for under that key.
 * 3. A ROLE THAT CANNOT SWAP is told so; nothing is sent.
 */
import { act, fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

type FetchInit = { method?: string; body?: string; headers?: Record<string, string> }

const apiFetch = vi.fn<(path: string, init?: FetchInit) => Promise<unknown>>()
let role = 'ward_clerk'

vi.mock('../../api/client', () => ({
  apiFetch: (path: string, init?: FetchInit) => apiFetch(path, init),
  errorDetailMessage: (detail: unknown, status: number) => {
    if (typeof detail === 'string') return detail
    const message = (detail as { message?: unknown } | null)?.message
    return typeof message === 'string' ? message : `The server answered ${status}.`
  },
  getUser: () => ({ role }),
}))

import * as swapActionModule from './actions/SwapAction'
import SwapAction from './actions/SwapAction'
import { actionsForTile, discoverTileActions, TILE_ACTIONS } from './actions'
import type { BedTile, BoardWard } from '../../api/adt/board'

function tile(over: Partial<BedTile> & { name?: string; aid?: string; linked?: boolean } = {}): BedTile {
  const { name = 'Meera Nair', aid = 'ADM-7', linked = true, ...rest } = over
  return {
    bed_id: 'BED-4', unit_code: 'GM-1/4', bed_label: '4', ward_id: 'WRD-GM', room_id: null, room: null,
    room_name: null, bed_class: null, isolation: false, temporary: false, status: 'occupied',
    status_words: 'Occupied', tone: 'occupied', markers: [], free: false, housekeeping_state: 'occupied',
    since: null, minutes_in_state: null, occupant_hidden: false,
    occupant: { admission_id: aid, patient_id: `P-${aid}`, name, mrn: `MRN-${aid}`,
                department: 'General Medicine', boarder: false, boarder_from: null,
                presence_status: null, linked },
    ...rest,
  }
}

const SELF = tile()
const PARTNER = tile({ bed_id: 'BED-5', unit_code: 'GM-1/5', bed_label: '5', name: 'Ravi Kumar',
  aid: 'ADM-8' })
const TYPED = tile({ bed_id: 'BED-6', unit_code: 'GM-1/6', bed_label: '6', name: 'Typed Bed',
  aid: 'ADM-9', linked: false })
const HIDDEN = tile({ bed_id: 'BED-7', unit_code: 'GM-1/7', bed_label: '7' })
HIDDEN.occupant = null
HIDDEN.occupant_hidden = true

const WARD = {
  id: 'WRD-GM', code: 'GM-1', name: null, department: 'General Medicine', ward_type: 'general',
  is_active: true, cleaning_tracked: true, register_complete: true, counts: {},
  tiles: [SELF, PARTNER, TYPED, HIDDEN],
} as BoardWard

const BOARD = {
  generated_at: '2026-10-09T10:00:00Z', department: 'General Medicine', ward_id: null,
  ward_choices: [], wards: [WARD], status_words: {},
}

const OPTIONS = {
  departments: ['General Medicine'],
  master: { backtime_hours: 72, self_authoriser: null, authorisers: [], wards: [] },
}

const SWAPPED = {
  replayed: false, swap_key: 'k',
  first: { replayed: false, admission_id: 'ADM-7', movement: { id: 'MOV-1', movement_type: 'bed_swap' },
           current_ward: 'GM-1', current_bed: '5', boarder: false },
  second: { replayed: false, admission_id: 'ADM-8', movement: { id: 'MOV-2', movement_type: 'bed_swap' },
            current_ward: 'GM-1', current_bed: '4', boarder: false },
}

function refusal(status: number, detail: unknown) {
  return Object.assign(new Error((detail as { message?: string })?.message ?? 'refused'),
    { status, detail })
}

function routeFetch(swap?: () => unknown) {
  apiFetch.mockImplementation(async (path: string) => {
    if (path.startsWith('/adt/board')) return BOARD
    if (path === '/adt/move-options') return OPTIONS
    if (path === '/adt/bed-swaps' && swap) return swap()
    return {}
  })
}

function posts(prefix: string) {
  return apiFetch.mock.calls.filter(([p, init]) => p.startsWith(prefix) && init?.method === 'POST')
}

function body(call: [string, FetchInit?]) {
  return JSON.parse(call[1]?.body ?? '{}')
}

async function renderAction(t: BedTile = SELF) {
  const onDone = vi.fn()
  await act(async () => {
    render(<SwapAction tile={t} ward={WARD} onDone={onDone} onClose={vi.fn()} />)
  })
  return { onDone }
}

beforeEach(() => {
  apiFetch.mockReset()
  role = 'ward_clerk'
})

describe('Swap beds tile action', () => {
  it('is installed as Swap beds, on occupied tiles whose patient the caller may see', () => {
    const entry = TILE_ACTIONS.find((a) => a.id === 'swap')
    expect(entry?.label).toBe('Swap beds')
    expect(entry?.needsOccupant).toBe(true)
    const entries = discoverTileActions({ './SwapAction.tsx': swapActionModule })
    const any = () => true
    expect(actionsForTile(entries, SELF, any)).toHaveLength(1)
    expect(actionsForTile(entries, HIDDEN, any)).toHaveLength(0)
    expect(actionsForTile(entries, tile({ status: 'available', occupant: null }), any)).toHaveLength(0)
  })

  it('lists only other patients the caller may see, in beds of bed setup', async () => {
    routeFetch()
    await renderAction()
    const options = Array.from(screen.getByLabelText('Swap with').querySelectorAll('option'))
      .map((o) => o.textContent)
    expect(options).toEqual(['Select...', 'GM-1, bed 5: Ravi Kumar (MRN-ADM-8)'])
  })

  it('posts both admissions and both beds the board showed, with one key', async () => {
    let calls = 0
    routeFetch(() => {
      calls += 1
      if (calls === 1) {
        throw refusal(409, { error: 'bed_not_clean_reason_required',
          message: 'Neither bed is cleaned between the two patients.' })
      }
      return SWAPPED
    })
    const { onDone } = await renderAction()
    fireEvent.change(screen.getByLabelText('Swap with'), { target: { value: 'BED-5' } })
    const send = screen.getByRole('button', { name: 'Record swap' })
    expect(send).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Reason for the swap'), { target: { value: 'Nearer the station' } })
    await act(async () => { fireEvent.click(send) })
    expect(screen.getByRole('alert')).toHaveTextContent('Neither bed is cleaned')
    fireEvent.change(screen.getByLabelText(/Reason they swap first/), { target: { value: 'Linen changed' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Record swap' })) })
    const sent = posts('/adt/bed-swaps')
    expect(sent).toHaveLength(2)
    expect(body(sent[0])).toEqual({
      first_admission_id: 'ADM-7', second_admission_id: 'ADM-8',
      expected_first_bed_id: 'BED-4', expected_second_bed_id: 'BED-5', reason: 'Nearer the station',
    })
    expect(body(sent[1])).toMatchObject({ not_clean_reason: 'Linen changed' })
    expect(sent[0][1]?.headers?.['Idempotency-Key']).toBeTruthy()
    expect(sent[1][1]?.headers?.['Idempotency-Key']).toBe(sent[0][1]?.headers?.['Idempotency-Key'])
    expect(screen.getByText('Beds swapped')).toBeInTheDocument()
    expect(screen.getByText('Meera Nair is now at GM-1, bed 5.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    expect(onDone).toHaveBeenCalledTimes(1)
  })

  it('tells a role that cannot swap, and a typed bed, what to do instead', async () => {
    role = 'nurse'
    routeFetch()
    await renderAction()
    expect(screen.getByText('Your role cannot swap beds.')).toBeInTheDocument()
    expect(apiFetch).not.toHaveBeenCalled()
    role = 'ward_clerk'
    await renderAction(TYPED)
    expect(screen.getByText(/bed is typed, not linked to bed setup/)).toBeInTheDocument()
  })
})
