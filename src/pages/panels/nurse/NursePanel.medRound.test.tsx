/**
 * The medication round is off on an Orb Ward Record computer (9 October 2026).
 *
 * The profile has no drug checks, no interlock and no orders (SALES_PLAN
 * section 3, step 2). Under it the server sends the shift board with
 * medication_round false, no medication tasks and null dose counts, and the
 * screen draws no dose task, no "doses overdue" or "blocked by allergy" chip,
 * no "Allergy block on N doses" line and no Med round button, even if a board
 * cached before the switch still carries doses. With scoring switched off
 * alone the round is drawn as before.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MedTask, NursePatientCard, ShiftBoard } from '../../../api/nurse'
import { clearOfflineCache } from '../../../lib/offline/phiCache'
import { resetClinicalSwitches, setClinicalSwitches } from '../../../shellConfig'

const getShiftBoard = vi.fn()

vi.mock('../../../api/nurse', () => ({
  getShiftBoard: (...a: unknown[]) => getShiftBoard(...a),
  recordObservations: vi.fn(),
  previewNews2: vi.fn(),
  currentUserName: () => 'Nurse A',
}))

import NursePanel from './NursePanel'

const AMOXICILLIN: MedTask = {
  medication_id: 41, drug_name: 'Amoxicillin', dosage: '500mg', route: 'Oral', frequency: 'Q6H',
  schedule_kind: 'scheduled', interval_minutes: 360, status: 'active', last_given_at: null,
  given_count: 0, due_at: new Date(Date.now() - 60 * 60_000).toISOString(), due_basis: 'order_start',
  missed_slots: 1, minutes_until_due: -60, overdue: true, due_soon: false, unverified: false,
  allergy: { decision: 'block', screen_failed: false, alerts: [{ substance: 'Penicillin' }], override: null },
}

function card(over: Partial<NursePatientCard> = {}): NursePatientCard {
  return {
    patient_id: 'P001', name: 'Asha Menon', mrn: 'MRN-1', bed: 'GM-07', ward: 'GM', age: 61,
    sex: 'F', diagnosis: 'Pneumonia', department: 'General Medicine', attending_doctor_name: null,
    on_scale_2: false, allergies: [{ substance: 'Penicillin' } as NursePatientCard['allergies'][number]],
    news2: null,
    last_obs_at: new Date(Date.now() - 130 * 60_000).toISOString(),
    obs_interval_minutes: 240, obs_band: null, obs_response: null,
    obs_due_at: new Date(Date.now() + 20 * 60_000).toISOString(), obs_minutes_until_due: 20,
    obs_due_source: 'ward_interval', obs_due_note: null,
    obs_overdue: false, obs_due_soon: true,
    medications: [AMOXICILLIN], meds_overdue: 1, meds_due_soon: 0, meds_allergy_blocked: 1,
    ...over,
  }
}

function board(patients: NursePatientCard[], over: Partial<ShiftBoard> = {}): ShiftBoard {
  return {
    generated_at: new Date().toISOString(),
    department: 'General Medicine',
    scoring_enabled: false,
    medication_round: true,
    patients,
    totals: {
      patients: patients.length, critical: null, elevated: null, obs_overdue: 0, meds_overdue: 1,
      meds_allergy_blocked: 1,
    },
    ...over,
  }
}

/** The board an Orb Ward Record server sends. */
const WARD_RECORD_BOARD = board(
  [card({ medications: [], meds_overdue: null, meds_due_soon: null, meds_allergy_blocked: null })],
  {
    medication_round: false,
    totals: { patients: 1, critical: null, elevated: null, obs_overdue: 0, meds_overdue: null, meds_allergy_blocked: null },
  },
)

async function renderPanel(b: ShiftBoard) {
  getShiftBoard.mockResolvedValue(b)
  render(<NursePanel />)
  await screen.findByText(/patients$/)
}

function expectNoMedicationRound() {
  const page = document.body.textContent ?? ''
  expect(page).not.toMatch(/doses? overdue/)
  expect(page).not.toMatch(/blocked by allergy/)
  expect(page).not.toMatch(/Amoxicillin/)
  expect(page).not.toMatch(/Medication due times/)
  expect(screen.queryByRole('button', { name: /medication round/ })).toBeNull()
  fireEvent.click(screen.getByRole('tab', { name: /My patients/ }))
  const tile = document.querySelector('.patient-card') as HTMLElement
  expect(tile.textContent).not.toMatch(/Allergy block on|prescriber authorisation|Doses:|No medications charted/)
  expect(screen.queryByRole('button', { name: 'Med round' })).toBeNull()
  // The allergy is a fact about the patient and stays on the card.
  expect(tile.textContent).toMatch(/Allergies: Penicillin/)
}

beforeEach(() => {
  getShiftBoard.mockReset()
  clearOfflineCache()
  localStorage.setItem('orb_user', JSON.stringify({ id: 'NUR001', name: 'Nurse A', role: 'nurse' }))
})

afterEach(() => {
  cleanup()
  resetClinicalSwitches()
})

describe('the medication round on an Orb Ward Record computer', () => {
  it('draws no dose task, dose count, allergy block line or Med round button', async () => {
    act(() => { setClinicalSwitches({ ward_record: true, scoring_enabled: false, models_enabled: false }) })
    await renderPanel(WARD_RECORD_BOARD)
    expectNoMedicationRound()
  })

  it('draws none of it from a board that still carries doses, while the profile is on', async () => {
    act(() => { setClinicalSwitches({ ward_record: true, scoring_enabled: false, models_enabled: false }) })
    await renderPanel(board([card()]))
    expectNoMedicationRound()
  })

  it('draws none of it when the server says there is no round, before the switch answers', async () => {
    await renderPanel(WARD_RECORD_BOARD)
    expectNoMedicationRound()
  })

  it('keeps the round with scoring switched off alone', async () => {
    act(() => { setClinicalSwitches({ scoring_enabled: false }) })
    await renderPanel(board([card()]))
    const totals = document.querySelector('.nurse-totals') as HTMLElement
    expect(totals.textContent).toMatch(/dose overdue/)
    expect(totals.textContent).toMatch(/blocked by allergy/)
    expect(screen.getByRole('button', { name: /open the medication round/ })).toBeTruthy()
    fireEvent.click(screen.getByRole('tab', { name: /My patients/ }))
    expect(screen.getByText(/Allergy block on 1 dose/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Med round' })).toBeTruthy()
  })
})
