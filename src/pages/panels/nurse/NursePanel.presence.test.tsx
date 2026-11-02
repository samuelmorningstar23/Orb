/**
 * 'NOT ON WARD SINCE' ON THE NURSE BOARD (adt-10), DISPLAY ONLY.
 *
 * With Beds and admissions on, the shift board's cards carry presence_status
 * and presence_since; the queue row and the patient card then say 'Not on
 * ward since' in local time. The task is still in the queue with its due
 * time: pausing it for a patient off the ward is adt-11. With the module off
 * the cards carry neither and nothing is drawn.
 */
import { render, screen, fireEvent, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { NursePatientCard, ShiftBoard } from '../../../api/nurse'
import { clearOfflineCache } from '../../../lib/offline/phiCache'
import { formatClinicalDateTime, formatClinicalTime } from '../../../lib/datetime'

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

function board(patients: NursePatientCard[]): ShiftBoard {
  return {
    generated_at: new Date().toISOString(),
    department: 'General Medicine',
    patients,
    totals: { patients: patients.length, critical: 0, elevated: 0, obs_overdue: patients.length,
              meds_overdue: 0, meds_allergy_blocked: 0 },
  }
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

describe('not on ward on the nurse board', () => {
  it.each([0, 1])('says the patient left %s days ago on the queue and card, and keeps the overdue task', async days => {
    const since = new Date(Date.parse(SINCE) - days * 86_400_000).toISOString()
    await renderPanel(board([patient({ presence_status: 'on_leave', presence_since: since })]))
    const when = days ? formatClinicalDateTime(since) : formatClinicalTime(since)
    const words = `Not on ward since ${when} (on leave)`
    const row = screen.getByRole('button', { name: /record observations/ })
    expect(within(row).getByText(words)).toBeTruthy()
    expect(row.getAttribute('aria-label')).toContain(`not on ward since ${when}`)
    fireEvent.click(screen.getByRole('tab', { name: /My patients/ }))
    expect(screen.getByTestId('not-on-ward').textContent).toBe(words)
  })

  it('draws nothing when the cards carry no presence (Beds and admissions off)', async () => {
    await renderPanel(board([patient()]))
    expect(screen.queryByText(/Not on ward/)).toBeNull()
    fireEvent.click(screen.getByRole('tab', { name: /My patients/ }))
    expect(screen.queryByTestId('not-on-ward')).toBeNull()
  })
})
