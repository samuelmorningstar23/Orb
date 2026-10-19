/**
 * The bed board's tile actions for the ward clerk's lanes (adt-17b; rulings
 * ADT-S3, ADT-S5 (a) and ADT-S8 (b), build defaults for Samuel to confirm):
 * 'Left the ward', 'Ordered transfer' and the doctor's 'Record admission
 * assessment'.
 *
 * What they must never get wrong:
 *
 * 1. WHO AND WHERE. 'Left the ward' and 'Ordered transfer' are the ward
 *    clerk's ('role:ward_clerk'); 'Left the ward' says plainly that it is not
 *    recorded anywhere but an Orb Ward Record computer. The assessment is the
 *    doctor's (patient.write) and is offered only on a tile the board marks
 *    'Admission assessment due'.
 * 2. THE READ-BACK. 'Left the ward' records nothing until the MRN typed
 *    matches, and sends one Idempotency-Key per screen.
 * 3. THE ORDERING DOCTOR. The ordered transfer cannot be sent without naming
 *    the doctor; a boarder's words are shown and confirmed, and the retry
 *    carries the same key.
 * 4. NEVER 'NONE' BY DEFAULT. The assessment starts with no allergy answer
 *    chosen and no code status chosen.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

type FetchInit = { method?: string; body?: string; headers?: Record<string, string> }

const apiFetch = vi.fn<(path: string, init?: FetchInit) => Promise<unknown>>()
let role = 'ward_clerk'

vi.mock('../../api/client', () => ({
  apiFetch: (path: string, init?: FetchInit) => apiFetch(path, init),
  errorDetailMessage: (detail: unknown, status: number) => {
    const message = (detail as { message?: unknown } | null)?.message
    return typeof message === 'string' ? message : `The server answered ${status}.`
  },
  getUser: () => ({ id: 'WC1', name: 'Clerk', role }),
}))

import { ASSESSMENT_DUE_MARKER } from '../../api/adt/clerk'
import type { BedTile, BoardWard } from '../../api/adt/board'
import { resetClinicalSwitches, setClinicalSwitches } from '../../shellConfig'
import { actionsForTile, discoverTileActions } from './actions'
import AssessmentAction, * as assessmentModule from './actions/AssessmentAction'
import DepartAction, * as departModule from './actions/DepartAction'
import OrderedTransferAction, * as orderedModule from './actions/OrderedTransferAction'

const WARD: BoardWard = {
  id: 'WRD-1', code: 'GM-1', name: null, department: 'General Medicine', ward_type: 'general',
  is_active: true, cleaning_tracked: false, register_complete: true,
  counts: { beds: 1, free: 0, occupied_total: 1 }, tiles: [],
}

function tile(over: Partial<BedTile> = {}): BedTile {
  return {
    bed_id: 'BED-4', unit_code: 'GM-1/4', bed_label: '4', ward_id: 'WRD-1', room_id: null,
    room: null, room_name: null, bed_class: null, isolation: false, temporary: false,
    status: 'occupied', status_words: 'Occupied', tone: 'occupied', markers: [], free: false,
    housekeeping_state: null, since: null, minutes_in_state: null,
    occupant: { admission_id: 'ADM-4', patient_id: 'P4', name: 'Asha Rao', mrn: 'HN-0004',
                department: 'General Medicine', boarder: false, boarder_from: null,
                presence_status: null, linked: true },
    occupant_hidden: false, ...over,
  }
}

function refusal(status: number, detail: unknown) {
  return Object.assign(new Error((detail as { message?: string })?.message ?? 'refused'),
    { status, detail })
}

function posts(prefix: string) {
  return apiFetch.mock.calls.filter(([p, init]) => p.startsWith(prefix) && init?.method === 'POST')
}

beforeEach(() => {
  resetClinicalSwitches()
  role = 'ward_clerk'
  apiFetch.mockReset()
  apiFetch.mockImplementation(async (path: string) => {
    if (path === '/adt/meta') return { module: 'adt', state: 'on', settings: { backtime_hours: 72 } }
    if (path === '/adt/move-options') return { master: {}, departments: ['General Medicine', 'Cardiology'] }
    if (path === '/admissions/clerk-options') {
      return { doctors: [{ id: 'D001', name: 'Dr. Test', department: 'General Medicine' }] }
    }
    return {}
  })
})

afterEach(() => {
  resetClinicalSwitches()
  vi.restoreAllMocks()
})

describe('which tiles offer which action', () => {
  const entries = discoverTileActions({
    './DepartAction.tsx': departModule,
    './OrderedTransferAction.tsx': orderedModule,
    './AssessmentAction.tsx': assessmentModule,
  })
  const ids = (t: BedTile, holds: (p: string) => boolean) =>
    actionsForTile(entries, t, holds).map((e) => e.id)

  it('offers the clerk her two actions, never the assessment', () => {
    const clerk = (p: string) => p === 'role:ward_clerk' || p === 'adt.bed'
    expect(ids(tile({ markers: [ASSESSMENT_DUE_MARKER] }), clerk)).toEqual(['ordered-transfer', 'left-ward'])
    expect(ids(tile({ status: 'occupied_on_leave' }), clerk)).toEqual([])
    expect(ids(tile({ occupant: null, occupant_hidden: true }), clerk)).toEqual([])
  })

  it('offers the doctor the assessment only on a tile marked due', () => {
    const doctor = (p: string) => p === 'patient.write'
    expect(ids(tile({ markers: [ASSESSMENT_DUE_MARKER] }), doctor)).toEqual(['admission-assessment'])
    expect(ids(tile(), doctor)).toEqual([])
  })

  it('offers the administrator neither clerk action', () => {
    const admin = (p: string) => p === 'adt.bed' || p === 'adt.transfer'
    expect(ids(tile({ markers: [ASSESSMENT_DUE_MARKER] }), admin)).toEqual([])
  })
})

describe('Left the ward', () => {
  it('says it is not recorded here off an Orb Ward Record computer', async () => {
    await act(async () => {
      render(<DepartAction tile={tile()} ward={WARD} onDone={vi.fn()} onClose={vi.fn()} />)
    })
    expect(screen.getByText(/a doctor's discharge ends the stay/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Record left the ward' })).toBeNull()
  })

  it('records only after the MRN is typed back, with one key', async () => {
    setClinicalSwitches({ ward_record: true })
    apiFetch.mockImplementation(async (path: string, init?: FetchInit) => {
      if (path === '/adt/meta') return { settings: { backtime_hours: 72 } }
      if (path === '/adt/admissions/ADM-4/left-ward' && init?.method === 'POST') {
        return { status: 'ended', replayed: false, left_ward: true, admission_id: 'ADM-4',
                 patient_id: 'P4', end_type: null, discharged_at: '2026-10-10T04:00:00Z',
                 movement_id: 'MOV-1', bed_freed: 'BED-4', discharge_link_issued: false }
      }
      return {}
    })
    const onDone = vi.fn()
    await act(async () => {
      render(<DepartAction tile={tile()} ward={WARD} onDone={onDone} onClose={vi.fn()} />)
    })
    const record = screen.getByRole('button', { name: 'Record left the ward' })
    expect(record).toBeDisabled()
    fireEvent.change(screen.getByLabelText(/Type the MRN/), { target: { value: 'HN-WRONG' } })
    expect(record).toBeDisabled()
    fireEvent.change(screen.getByLabelText(/Type the MRN/), { target: { value: 'hn-0004' } })
    expect(record).toBeEnabled()
    await act(async () => { fireEvent.click(record) })
    const sent = posts('/adt/admissions/ADM-4/left-ward')
    expect(sent).toHaveLength(1)
    expect(sent[0][1]?.headers?.['Idempotency-Key']).toMatch(/\S{8,}/)
    const body = JSON.parse(sent[0][1]?.body ?? '{}')
    expect(body.confirm_mrn).toBe('hn-0004')
    expect(body).not.toHaveProperty('end_type')
    expect(await screen.findByText(/no patient link was issued/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    expect(onDone).toHaveBeenCalled()
  })

  it("prints the server's words for a refusal", async () => {
    setClinicalSwitches({ ward_record: true })
    apiFetch.mockImplementation(async (path: string, init?: FetchInit) => {
      if (init?.method === 'POST') {
        throw refusal(409, { error: 'patient_not_on_ward',
                             message: 'This patient is recorded as on leave.' })
      }
      return { settings: { backtime_hours: 72 } }
    })
    await act(async () => {
      render(<DepartAction tile={tile()} ward={WARD} onDone={vi.fn()} onClose={vi.fn()} />)
    })
    fireEvent.change(screen.getByLabelText(/Type the MRN/), { target: { value: 'HN-0004' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Record left the ward' })) })
    expect(screen.getByRole('alert')).toHaveTextContent('This patient is recorded as on leave.')
  })
})

describe('Ordered transfer', () => {
  it('cannot be sent without the ordering doctor, and sends the order named', async () => {
    let calls = 0
    apiFetch.mockImplementation(async (path: string, init?: FetchInit) => {
      if (path === '/adt/move-options') return { master: {}, departments: ['General Medicine', 'Cardiology'] }
      if (path === '/admissions/clerk-options') {
        return { doctors: [{ id: 'D001', name: 'Dr. Test', department: 'General Medicine' }] }
      }
      if (path === '/adt/ordered-transfers' && init?.method === 'POST') {
        calls += 1
        if (calls === 1) {
          throw refusal(409, { error: 'boarder_acknowledgement_required',
                               message: 'Nurses on GM-1 will not be able to open this chart.' })
        }
        return { replayed: false, admission_id: 'ADM-4', current_department: 'Cardiology',
                 ordered_by: { id: 'D001', name: 'Dr. Test' } }
      }
      return {}
    })
    await act(async () => {
      render(<OrderedTransferAction tile={tile()} ward={WARD} onDone={vi.fn()} onClose={vi.fn()} />)
    })
    await waitFor(() => expect(screen.getByLabelText('Receiving department')).toBeInTheDocument())
    // Its own department is not a transfer.
    expect(screen.queryByRole('option', { name: 'General Medicine' })).toBeNull()
    const send = screen.getByRole('button', { name: 'Record transfer' })
    fireEvent.change(screen.getByLabelText('Receiving department'), { target: { value: 'Cardiology' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Accepted by cardiology' } })
    expect(send).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Ordered by'), { target: { value: 'D001' } })
    expect(send).toBeEnabled()
    await act(async () => { fireEvent.click(send) })
    expect(screen.getByText('Nurses on GM-1 will not be able to open this chart.')).toBeInTheDocument()
    expect(send).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: 'I have read this' }))
    await act(async () => { fireEvent.click(send) })
    const sent = posts('/adt/ordered-transfers')
    expect(sent).toHaveLength(2)
    const [first, second] = sent.map(([, init]) => init)
    expect(second?.headers?.['Idempotency-Key']).toBe(first?.headers?.['Idempotency-Key'])
    const body = JSON.parse(second?.body ?? '{}')
    expect(body).toMatchObject({ admission_id: 'ADM-4', to_department: 'Cardiology',
                                 ordered_by: 'D001', keep_location: true,
                                 expected_bed_id: 'BED-4', boarder_acknowledged: true })
    expect(await screen.findByText(/on the order of Dr. Test/)).toBeInTheDocument()
  })
})

describe('Record admission assessment', () => {
  it('starts with nothing chosen and sends what the doctor recorded', async () => {
    role = 'doctor'
    apiFetch.mockImplementation(async (path: string, init?: FetchInit) => {
      if (path === '/admissions/ADM-4/assessment' && init?.method === 'POST') {
        return { admission_id: 'ADM-4', patient_id: 'P4', charted: ['penicillin'], reactivated: [],
                 assessment: { id: 1, status: 'allergies_recorded', status_label: 'Allergies recorded',
                               assessed_at: '2026-10-10T04:00:00Z' }, assessment_due: false }
      }
      return {}
    })
    await act(async () => {
      render(<AssessmentAction tile={tile({ markers: [ASSESSMENT_DUE_MARKER] })} ward={WARD}
        onDone={vi.fn()} onClose={vi.fn()} />)
    })
    for (const radio of screen.getAllByRole('radio')) expect(radio).not.toBeChecked()
    expect(screen.getByLabelText('Code status')).toHaveValue('')
    const send = screen.getByRole('button', { name: 'Record assessment' })
    expect(send).toBeDisabled()
    fireEvent.click(screen.getByRole('radio', { name: 'Allergies recorded' }))
    fireEvent.change(screen.getByLabelText('Allergies the patient named'), { target: { value: 'Penicillin' } })
    fireEvent.change(screen.getByLabelText('Presenting complaint'), { target: { value: 'Breathless' } })
    fireEvent.change(screen.getByLabelText('Code status'), { target: { value: 'full_code' } })
    expect(send).toBeEnabled()
    await act(async () => { fireEvent.click(send) })
    const body = JSON.parse(posts('/admissions/ADM-4/assessment')[0][1]?.body ?? '{}')
    expect(body).toEqual({ allergy_status: 'allergies_recorded', allergies: ['Penicillin'],
                           chief_complaint: 'Breathless', code_status: 'full_code' })
    expect(await screen.findByText('Admission assessment recorded')).toBeInTheDocument()
  })

  it('says nothing is due on a tile the board does not mark', async () => {
    role = 'doctor'
    await act(async () => {
      render(<AssessmentAction tile={tile()} ward={WARD} onDone={vi.fn()} onClose={vi.fn()} />)
    })
    expect(screen.getByText(/No admission assessment is due/)).toBeInTheDocument()
  })
})
