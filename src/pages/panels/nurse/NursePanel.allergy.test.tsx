/**
 * THE ALLERGY PICTURE ON THE NURSE'S OWN SCREENS (F054, F055).
 *
 *   · An empty allergy list used to render NOTHING on the patient card. Blank
 *     reads as "nothing to know", and an empty list is also every patient
 *     nobody asked. It now says so, quietly, and never says NKDA.
 *   · The board already runs the bedside allergy interlock on every dose and
 *     counts the doses it will refuse (`meds_allergy_blocked`). That count
 *     reached no screen, so a nurse first met the block at the bedside.
 */
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react'
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
  await waitFor(() => expect(screen.getByRole('button', { name: /^Refresh$/ })).toBeEnabled())
}

beforeEach(() => {
  getShiftBoard.mockReset()
  clearOfflineCache()
  localStorage.setItem('orb_user', JSON.stringify({ id: 'NUR001', name: 'Nurse A', role: 'nurse' }))
})

describe('the patient card', () => {
  it('says "No allergies recorded" for an empty list, never NKDA and never nothing', async () => {
    await renderPanel(board([patient()]))
    fireEvent.click(screen.getByRole('tab', { name: /My patients/ }))
    expect(screen.getByText('No allergies recorded. Confirm with the patient.')).toBeTruthy()
    expect(screen.queryByText(/NKDA/)).toBeNull()
  })

  it('still lists charted allergies when there are some', async () => {
    await renderPanel(board([patient({
      allergies: [{ substance: 'Penicillin', reaction: 'anaphylaxis', severity: 'severe' }],
    })]))
    fireEvent.click(screen.getByRole('tab', { name: /My patients/ }))
    expect(screen.getByText('Allergies: Penicillin')).toBeTruthy()
    expect(screen.queryByText(/No allergies recorded/)).toBeNull()
  })

  it('names the doses the bedside allergy screen will refuse', async () => {
    await renderPanel(board([patient({ medications: [BLOCKED_DOSE], meds_allergy_blocked: 1 })], 1))
    fireEvent.click(screen.getByRole('tab', { name: /My patients/ }))
    expect(screen.getByText(/Allergy block on 1 dose: needs prescriber authorisation/)).toBeTruthy()
  })
})

describe('the shift totals and the task queue', () => {
  it('shows a blocked-by-allergy count only when there is one', async () => {
    await renderPanel(board([patient({ medications: [BLOCKED_DOSE], meds_allergy_blocked: 1 })], 1))
    const strip = screen.getByText(/^1 patients$/).parentElement!
    expect(within(strip).getByText('blocked by allergy')).toBeTruthy()
  })

  it('has no blocked-by-allergy chip on a board with none', async () => {
    await renderPanel(board([patient()]))
    expect(screen.queryByText('blocked by allergy')).toBeNull()
  })

  it('marks the due dose in the queue', async () => {
    await renderPanel(board([patient({ medications: [BLOCKED_DOSE], meds_allergy_blocked: 1 })], 1))
    const row = screen.getByRole('button', { name: /Cefuroxime/ })
    expect(within(row).getByText('Allergy block')).toBeTruthy()
    expect(row.getAttribute('aria-label')).toMatch(/allergy block, needs prescriber authorisation/)
  })
})
