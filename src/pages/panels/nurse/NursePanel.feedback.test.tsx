/**
 * THE CONFIRMATION AFTER A SHEET CLOSES (F133).
 *
 *   · It rendered in the page flow at the top of the panel. On a phone that is
 *     off-screen by the time a sheet closes, so the obs confirmation, which
 *     carries the escalation response, was never seen. It is now a fixed toast,
 *     portalled to the body so no ancestor can capture its position.
 *   · It cleared itself after six seconds. It now stays until dismissed or
 *     replaced.
 *   · "Charted as not given" flashed in the success green. Not given is a
 *     recorded decision, not an achievement, so it wears the caution tone.
 *
 * The two sheets are stubbed: what is under test is only what the panel says
 * with the record each hands back.
 */
import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MedTask, News2, NursePatientCard, ShiftBoard } from '../../../api/nurse'
import { clearOfflineCache } from '../../../lib/offline/phiCache'

const getShiftBoard = vi.fn()

vi.mock('../../../api/nurse', () => ({
  getShiftBoard: (...a: unknown[]) => getShiftBoard(...a),
  recordObservations: vi.fn(),
  previewNews2: vi.fn(),
  currentUserName: () => 'Nurse A',
}))

vi.mock('./MedRoundSheet', () => ({
  default: ({ onAdministered }: { onAdministered: (r: unknown) => void }) => (
    <div>
      <button
        type="button"
        onClick={() => onAdministered({ id: 1, medication_id: 21, drug_name: 'Cefuroxime', status: 'not_given' })}
      >
        Stub: chart not given
      </button>
      <button
        type="button"
        onClick={() => onAdministered({ id: 2, medication_id: 21, drug_name: 'Cefuroxime', status: 'given' })}
      >
        Stub: chart given
      </button>
    </div>
  ),
}))

vi.mock('./ObsEntrySheet', () => ({
  default: ({ onRecorded }: { onRecorded: (n: News2, s: unknown) => void }) => (
    <button
      type="button"
      onClick={() => onRecorded(
        {
          score: 0.5, level: 'elevated', raw_news2: 5, single_param_3: false, drivers: [],
          breakdown: [], scoring_model: 'NEWS2', spo2_scale: 1,
        },
        { interval_minutes: 60, band: 'Medium', response: 'Inform the nurse in charge.' },
      )}
    >
      Stub: save obs
    </button>
  ),
}))

import NursePanel from './NursePanel'

const DOSE: MedTask = {
  medication_id: 21, drug_name: 'Cefuroxime', dosage: '750 mg', route: 'IV', frequency: 'TDS',
  schedule_kind: 'scheduled', interval_minutes: 480, status: 'active', last_given_at: null,
  given_count: 0, due_at: null, due_basis: 'order_start', missed_slots: 0, minutes_until_due: 120,
  overdue: false, due_soon: false, unverified: false,
}

const PATIENT: NursePatientCard = {
  patient_id: 'P001', name: 'Asha Menon', mrn: 'MRN-1', bed: 'GM-07', ward: 'GM', age: 61,
  sex: 'F', diagnosis: 'Pneumonia', department: 'General Medicine', attending_doctor_name: null,
  on_scale_2: false, allergies: [], news2: null, last_obs_at: null, obs_interval_minutes: 240,
  obs_band: 'Low', obs_response: 'Routine', obs_due_at: null, obs_minutes_until_due: 60,
  obs_overdue: false, obs_due_soon: false, medications: [DOSE], meds_overdue: 0, meds_due_soon: 0,
}

const BOARD: ShiftBoard = {
  generated_at: new Date().toISOString(),
  department: 'General Medicine',
  patients: [PATIENT],
  totals: { patients: 1, critical: 0, elevated: 0, obs_overdue: 0, meds_overdue: 0 },
}

async function mountOnPatients() {
  getShiftBoard.mockResolvedValue(BOARD)
  const view = render(<NursePanel />)
  await act(async () => {})
  fireEvent.click(screen.getByRole('tab', { name: /My patients/ }))
  return view
}

const toast = () => document.body.querySelector('.nurse-toast')

beforeEach(() => {
  getShiftBoard.mockReset()
  clearOfflineCache()
  Object.defineProperty(navigator, 'onLine', { value: true, configurable: true })
  localStorage.setItem('orb_user', JSON.stringify({ id: 'NUR001', name: 'Nurse A', role: 'nurse' }))
})

afterEach(() => {
  vi.useRealTimers()
  clearOfflineCache()
  localStorage.clear()
})

describe('the confirmation toast', () => {
  it('flashes "not given" in the caution tone, never the success green', async () => {
    await mountOnPatients()
    fireEvent.click(screen.getByRole('button', { name: 'Med round' }))
    fireEvent.click(screen.getByRole('button', { name: 'Stub: chart not given' }))

    const el = toast()!
    expect(el.textContent).toContain('Cefuroxime charted as not given.')
    expect(el.className).toContain('nurse-note--warn')
    expect(el.className).not.toContain('nurse-note--ok')
  })

  it('flashes "given" in the success tone', async () => {
    await mountOnPatients()
    fireEvent.click(screen.getByRole('button', { name: 'Med round' }))
    fireEvent.click(screen.getByRole('button', { name: 'Stub: chart given' }))

    expect(toast()!.className).toContain('nurse-note--ok')
  })

  it('is pinned outside the page flow and stays until dismissed', async () => {
    const { container } = await mountOnPatients()
    fireEvent.click(screen.getAllByRole('button', { name: 'Record obs' })[0])

    // The old six-second timer would be created by this click, so install the
    // fake clock first and it is the one advanced below.
    vi.useFakeTimers()
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Stub: save obs' })) })

    const el = toast()!
    expect(el).not.toBeNull()
    // Portalled: in the panel's flow it sat at the top, off-screen on a phone.
    expect(container.contains(el)).toBe(false)
    expect(el.getAttribute('role')).toBe('status')
    expect(el.textContent).toContain('Observations recorded for Asha Menon. NEWS2 5 (elevated). Inform the nurse in charge.')

    act(() => { vi.advanceTimersByTime(30_000) })
    expect(toast()).not.toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(toast()).toBeNull()
  })
})
