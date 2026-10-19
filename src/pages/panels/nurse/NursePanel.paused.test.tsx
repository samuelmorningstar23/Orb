/**
 * PAUSED CLOCKS FOR A PATIENT OFF THE WARD (adt-11, ruling ADT-S4 (a)).
 *
 * With Beds and admissions on, the server pauses the obs and dose clocks of a
 * patient recorded on leave or absconded: nothing of theirs is overdue or due,
 * so nothing of theirs is in the Due now queue. They are not dropped: a "Not
 * on the ward" list under the queue names them with the reason, the totals say
 * how many, and their card on My patients says why its clocks are stopped.
 * With the module off the board carries no `paused` key and no count, and
 * nothing here is drawn.
 */
import { render, screen, fireEvent, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MedTask, NursePatientCard, ShiftBoard } from '../../../api/nurse'
import { clearOfflineCache } from '../../../lib/offline/phiCache'
import { formatClinicalTime } from '../../../lib/datetime'

const getShiftBoard = vi.fn()

vi.mock('../../../api/nurse', () => ({
  getShiftBoard: (...a: unknown[]) => getShiftBoard(...a),
  recordObservations: vi.fn(),
  previewNews2: vi.fn(),
  currentUserName: () => 'Nurse A',
}))

import NursePanel from './NursePanel'

const NOW = new Date(2026, 9, 10, 12).getTime()
const SINCE = new Date(NOW - 50 * 60_000).toISOString()
const REASON = 'Observations and doses are paused while the patient is on leave. '
  + 'They restart when the return to the ward is recorded.'

const DOSE = {
  medication_id: 7, drug_name: 'Paracetamol', dosage: '1 g', route: 'Oral', frequency: 'Q6H',
  schedule_kind: 'scheduled', interval_minutes: 360, status: 'active', last_given_at: null,
  given_count: 0, due_at: null, due_basis: 'order_start', missed_slots: 0,
  minutes_until_due: null, overdue: false, due_soon: false, unverified: false,
  allergy: { decision: 'allow' }, paused: true,
} as unknown as MedTask

function patient(over: Partial<NursePatientCard> & Record<string, unknown> = {}): NursePatientCard {
  return {
    patient_id: 'P001', name: 'Asha Menon', mrn: 'MRN-1', bed: 'GM-07', ward: 'GM', age: 61,
    sex: 'F', diagnosis: 'Pneumonia', department: 'General Medicine', attending_doctor_name: null,
    on_scale_2: false, allergies: [], news2: null, last_obs_at: null, obs_interval_minutes: 240,
    obs_band: 'Low', obs_response: 'Routine', obs_due_at: null, obs_minutes_until_due: -20,
    obs_overdue: true, obs_due_soon: false, medications: [], meds_overdue: 0, meds_due_soon: 0,
    ...over,
  } as NursePatientCard
}

const onWard = () => patient({ presence_status: null, presence_since: null, paused: null })
const onLeave = () => patient({
  patient_id: 'P002', name: 'Ravi Kumar', bed: 'GM-08',
  presence_status: 'on_leave', presence_since: SINCE,
  paused: { presence_status: 'on_leave', presence_since: SINCE, reason: REASON },
  obs_overdue: false, obs_minutes_until_due: null, medications: [DOSE],
  meds_overdue: 0, meds_due_soon: 0,
})

function board(patients: NursePatientCard[], extra: Record<string, unknown> = {}): ShiftBoard {
  return {
    generated_at: new Date().toISOString(),
    department: 'General Medicine',
    patients,
    totals: { patients: patients.length, critical: 0, elevated: 0,
              obs_overdue: patients.filter(p => p.obs_overdue).length,
              meds_overdue: 0, meds_allergy_blocked: 0, ...extra },
  } as ShiftBoard
}

async function renderPanel(b: ShiftBoard) {
  getShiftBoard.mockResolvedValue(b)
  render(<NursePanel />)
  await screen.findByText(/patients$/)
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
  getShiftBoard.mockReset()
  clearOfflineCache()
  localStorage.setItem('orb_user', JSON.stringify({ id: 'NUR001', name: 'Nurse A', role: 'nurse' }))
})

afterEach(() => { vi.useRealTimers() })

describe('paused clocks for a patient off the ward', () => {
  it('keeps them out of the queue and lists them with the reason', async () => {
    await renderPanel(board([onWard(), onLeave()], { not_on_ward: 1 }))
    // The patient on the ward still owes observations.
    const due = screen.getAllByRole('button', { name: /record observations/ })
    expect(due).toHaveLength(1)
    expect(due[0].getAttribute('aria-label')).toContain('Asha Menon')
    // The patient on leave is listed, never dropped, with why and since when.
    const heading = screen.getByRole('heading', { name: /Not on the ward/ })
    expect(heading.textContent).toContain('1')
    const row = screen.getByTestId('paused-row')
    expect(within(row).getByText('GM-08 · Ravi Kumar')).toBeTruthy()
    expect(within(row).getByText(REASON)).toBeTruthy()
    expect(within(row).getByText(`Not on ward since ${formatClinicalTime(SINCE)} (on leave)`))
      .toBeTruthy()
    expect(row.tagName).toBe('DIV')
    // The totals say how many, untinted.
    const chip = screen.getByText('not on ward').parentElement as HTMLElement
    expect(chip.className).toBe('orb-count-chip')
    expect(chip.textContent).toContain('1')
  })

  it('says on the card why its clocks are stopped', async () => {
    await renderPanel(board([onWard(), onLeave()], { not_on_ward: 1 }))
    fireEvent.click(screen.getByRole('tab', { name: /My patients/ }))
    expect(screen.getByText(REASON)).toBeTruthy()
    expect(screen.getByText('Doses: 1 charted · paused while off the ward')).toBeTruthy()
  })

  it('draws nothing of this with Beds and admissions off', async () => {
    await renderPanel(board([patient()]))
    expect(screen.queryByRole('heading', { name: /Not on the ward/ })).toBeNull()
    expect(screen.queryByTestId('paused-row')).toBeNull()
    expect(screen.queryByText('not on ward')).toBeNull()
    fireEvent.click(screen.getByRole('tab', { name: /My patients/ }))
    expect(screen.queryByText(/paused/)).toBeNull()
  })
})
