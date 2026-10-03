/**
 * THE SIMULATED-VITALS HONESTY LABEL ON THE NURSE BOARD (F043).
 *
 * Under simulation every NEWS2 on the board was scored from a synthetic row
 * and nothing said so. The row and the card now say "Simulated vitals"
 * whenever the score came from the simulator, and nothing otherwise.
 */
import { render, screen, fireEvent, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { MedTask, NursePatientCard, ShiftBoard } from '../../../api/nurse'
import { clearOfflineCache } from '../../../lib/offline/phiCache'

const getShiftBoard = vi.fn()

vi.mock('../../../api/nurse', () => ({
  getShiftBoard: (...a: unknown[]) => getShiftBoard(...a),
  recordObservations: vi.fn(),
  previewNews2: vi.fn(),
  currentUserName: () => 'Nurse A',
}))

import NursePanel from './NursePanel'

const BLOCKED_DOSE: MedTask = {
  medication_id: 21, drug_name: 'Cefuroxime', dosage: '750 mg', route: 'IV', frequency: 'TDS',
  schedule_kind: 'scheduled', interval_minutes: 480, status: 'active', last_given_at: null,
  given_count: 0, due_at: new Date(Date.now() + 10 * 60_000).toISOString(),
  due_basis: 'order_start', missed_slots: 0, minutes_until_due: 10, overdue: false,
  due_soon: true, unverified: false,
  allergy: {
    decision: 'block', screen_failed: false, override: null,
    alerts: [{ substance: 'Penicillin', decision: 'block', effective_severity: 'severe' }],
  },
}

function patient(over: Partial<NursePatientCard> = {}): NursePatientCard {
  return {
    patient_id: 'P001', name: 'Asha Menon', mrn: 'MRN-1', bed: 'GM-07', ward: 'GM', age: 61,
    sex: 'F', diagnosis: 'Pneumonia', department: 'General Medicine', attending_doctor_name: null,
    on_scale_2: false, allergies: [], news2: null, last_obs_at: null, obs_interval_minutes: 240,
    obs_band: 'Low', obs_response: 'Routine', obs_due_at: null, obs_minutes_until_due: 60,
    obs_overdue: false, obs_due_soon: false, medications: [], meds_overdue: 0, meds_due_soon: 0,
    ...over,
  }
}

function board(patients: NursePatientCard[], blocked = 0): ShiftBoard {
  return {
    generated_at: new Date().toISOString(),
    department: 'General Medicine',
    patients,
    totals: {
      patients: patients.length, critical: 0, elevated: 0, obs_overdue: 0, meds_overdue: 0,
      meds_allergy_blocked: blocked,
    },
  }
}

async function renderPanel(b: ShiftBoard) {
  getShiftBoard.mockResolvedValue(b)
  render(<NursePanel />)
  await screen.findByText(/patients$/)
}

beforeEach(() => {
  getShiftBoard.mockReset()
  clearOfflineCache()
  localStorage.setItem('orb_user', JSON.stringify({ id: 'NUR001', name: 'Nurse A', role: 'nurse' }))
})

describe('simulated vitals', () => {
  it('labels the queue row and the card when the score came from the simulator', async () => {
    const card = patient({ vitals_source: 'simulated', medications: [BLOCKED_DOSE] })
    await renderPanel(board([card]))
    const row = screen.getByRole('button', { name: /Cefuroxime/ })
    expect(within(row).getByText('Simulated vitals')).toBeTruthy()
    expect(row.getAttribute('aria-label')).toMatch(/scored from simulated vitals/)
    fireEvent.click(screen.getByRole('tab', { name: /My patients/ }))
    expect(screen.getByText(/Simulated vitals: the score above is not from this patient/)).toBeTruthy()
  })

  it('says nothing for a set a nurse took', async () => {
    await renderPanel(board([patient({ vitals_source: 'nurse_manual', medications: [BLOCKED_DOSE] })]))
    expect(screen.queryByText('Simulated vitals')).toBeNull()
    fireEvent.click(screen.getByRole('tab', { name: /My patients/ }))
    expect(screen.queryByText(/Simulated vitals/)).toBeNull()
  })
})
