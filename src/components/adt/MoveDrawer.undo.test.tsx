/**
 * The Move screen's 'Recorded in error' choice (adt-6b).
 *
 * What it must never get wrong:
 *
 * 1. IT IS OFFERED ONLY WHEN THE SERVER SAYS THE LAST MOVE CAN BE UNDONE, and
 *    undoing a transfer (which gives the chart back) only to a role that may
 *    transfer: never to the ward clerk.
 * 2. IT POSTS TO THE MOVEMENT'S CORRECT ROUTE with the bed the screen read,
 *    the reason and the screen's one Idempotency-Key, and no move time.
 * 3. A TRANSFER UNDONE SAYS IT HANDS THE CHART BACK before the button.
 * 4. WHAT THE SERVER ASKS FOR OPENS HERE: a bed not recorded clean opens its
 *    reason field, and the retry carries it under the same key.
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
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

import MoveDrawer from './MoveDrawer'
import type { LastMove, MoveOptions } from '../../api/adt/moves'

const OPTIONS: MoveOptions = {
  departments: ['General Medicine', 'Surgery'],
  master: { backtime_hours: 72, self_authoriser: null, authorisers: [], wards: [] },
}

const BED_CHANGE: LastMove = {
  id: 'MOV-9', movement_type: 'bed_change', moved_at: '2026-10-08T09:00:00+00:00',
  moved_by_name: 'Ward Clerk', from: { department: 'General Medicine', ward: 'GM-1', bed: '4' },
  to: { department: 'General Medicine', ward: 'GM-1', bed: '5' }, changes_department: false,
  correctable: true, not_correctable: null,
}

function location(lastMove: LastMove | null) {
  return {
    admission_id: 'ADM-7', department: 'General Medicine', ward: 'GM-1', bed: '5', ward_id: 'WRD-GM',
    bed_id: 'BED-5', ward_department: 'General Medicine', linked: true, awaiting_bed: false,
    boarder: false, last_move: lastMove,
  }
}

function refusal(status: number, detail: unknown) {
  return Object.assign(new Error((detail as { message?: string })?.message ?? 'refused'),
    { status, detail })
}

function routeFetch(lastMove: LastMove | null, correct?: () => unknown) {
  apiFetch.mockImplementation(async (path: string) => {
    if (path === '/adt/move-options') return OPTIONS
    if (path === '/adt/admissions/ADM-7/location') return location(lastMove)
    if (path.startsWith('/adt/movements/') && correct) return correct()
    return {}
  })
}

function posts(prefix: string) {
  return apiFetch.mock.calls.filter(([p, init]) => p.startsWith(prefix) && init?.method === 'POST')
}

function body(call: [string, FetchInit?]) {
  return JSON.parse(call[1]?.body ?? '{}')
}

async function renderDrawer(props: { canTransfer?: boolean; canChangeBed?: boolean } = {}) {
  await act(async () => {
    render(
      <MoveDrawer
        admissionId="ADM-7"
        patient={{ name: 'Meera Nair', mrn: 'MRN-7' }}
        canTransfer={props.canTransfer ?? true}
        canChangeBed={props.canChangeBed ?? true}
        onDone={vi.fn()}
        onClose={vi.fn()}
      />,
    )
  })
}

const UNDONE = {
  replayed: false, admission_id: 'ADM-7', patient_id: 'P7', corrects_movement_id: 'MOV-9',
  movement: { id: 'MOV-10', movement_type: 'correction' },
  current_department: 'General Medicine', current_ward: 'GM-1', current_bed: '4',
  bed_id: 'BED-4', linked: true, awaiting_bed: false, boarder: false, department_changed: false,
}

beforeEach(() => {
  apiFetch.mockReset()
  role = 'doctor'
})

describe('MoveDrawer, recorded in error', () => {
  it('is not offered when the last move cannot be undone', async () => {
    routeFetch({ ...BED_CHANGE, correctable: false,
      not_correctable: { code: 'not_latest_movement', message: 'Only the latest move.' } })
    await renderDrawer()
    expect(screen.queryByText('Undo the last move, recorded in error')).toBeNull()
  })

  it('posts the bed the screen read, the reason and the key to the correct route', async () => {
    routeFetch(BED_CHANGE, () => UNDONE)
    await renderDrawer()
    fireEvent.click(screen.getByRole('radio', { name: 'Undo the last move, recorded in error' }))
    expect(screen.getByTestId('move-undo')).toHaveTextContent('From GM-1, bed 4 to GM-1, bed 5.')
    // No move time: a correction is recorded when it is sent.
    expect(screen.queryByLabelText('When the patient moved')).toBeNull()
    const send = screen.getByRole('button', { name: 'Undo the move' })
    expect(send).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Why the move was recorded in error'),
      { target: { value: 'Moved the wrong patient' } })
    await act(async () => { fireEvent.click(send) })
    const sent = posts('/adt/movements/MOV-9/correct')
    expect(sent).toHaveLength(1)
    expect(body(sent[0])).toEqual({ expected_bed_id: 'BED-5', reason: 'Moved the wrong patient' })
    expect(sent[0][1]?.headers?.['Idempotency-Key']).toBeTruthy()
    expect(screen.getByText('Move undone')).toBeInTheDocument()
    expect(screen.getByText('Now at GM-1, bed 4.')).toBeInTheDocument()
  })

  it('opens the reason for a bed not recorded clean and retries under the same key', async () => {
    let calls = 0
    routeFetch(BED_CHANGE, () => {
      calls += 1
      if (calls === 1) {
        throw refusal(409, { error: 'bed_not_clean_reason_required',
          message: 'Bed 4 on GM-1 has not been recorded clean.' })
      }
      return UNDONE
    })
    await renderDrawer()
    fireEvent.click(screen.getByRole('radio', { name: 'Undo the last move, recorded in error' }))
    fireEvent.change(screen.getByLabelText('Why the move was recorded in error'),
      { target: { value: 'Moved the wrong patient' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Undo the move' })) })
    expect(screen.getByRole('alert')).toHaveTextContent('has not been recorded clean')
    const field = screen.getByLabelText(/has not been recorded clean since/)
    expect(screen.getByRole('button', { name: 'Undo the move' })).toBeDisabled()
    fireEvent.change(field, { target: { value: 'The patient never left the bed' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Undo the move' })) })
    const sent = posts('/adt/movements/MOV-9/correct')
    expect(sent).toHaveLength(2)
    expect(body(sent[1])).toMatchObject({ not_clean_reason: 'The patient never left the bed' })
    expect(sent[1][1]?.headers?.['Idempotency-Key']).toBe(sent[0][1]?.headers?.['Idempotency-Key'])
  })

  it('says a transfer undone gives the chart back, and is never offered to the ward clerk', async () => {
    const transfer: LastMove = {
      ...BED_CHANGE, movement_type: 'transfer', changes_department: true,
      from: { department: 'Surgery', ward: 'SUR-1', bed: '2' },
      to: { department: 'General Medicine', ward: 'GM-1', bed: '5' },
    }
    routeFetch(transfer)
    await renderDrawer()
    fireEvent.click(screen.getByRole('radio', { name: 'Undo the last move, recorded in error' }))
    expect(screen.getByTestId('move-undo')).toHaveTextContent(
      'staff in Surgery can open it again, and staff in General Medicine can no longer see this patient')
    // The ward clerk holds adt.bed only: no department change, so no undo.
    cleanup()
    role = 'ward_clerk'
    routeFetch(transfer)
    await renderDrawer({ canTransfer: false })
    expect(screen.getByRole('button', { name: 'Record bed change' })).toBeInTheDocument()
    expect(screen.queryByText('Undo the last move, recorded in error')).toBeNull()
  })

  it('offers the ward clerk the undo of a bed change beside the bed change', async () => {
    role = 'ward_clerk'
    routeFetch(BED_CHANGE)
    await renderDrawer({ canTransfer: false })
    expect(screen.getByRole('radio', { name: 'Change bed, same department' })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Undo the last move, recorded in error' })).toBeInTheDocument()
    expect(screen.queryByText('Transfer to another department')).toBeNull()
  })
})
