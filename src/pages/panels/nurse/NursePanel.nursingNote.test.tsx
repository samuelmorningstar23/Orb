/**
 * The Nursing note button on the shift board's patient card (21 October
 * 2026): offered to a nurse on an ordinary site, opens the note sheet, and
 * absent under the Orb Ward Record profile, where nursing notes stay on paper
 * and the route is switched off. A doctor, who reads notes but writes none,
 * is not offered it either.
 */
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import type { NursePatientCard, ShiftBoard } from '../../../api/nurse'
import { clearOfflineCache } from '../../../lib/offline/phiCache'
import { resetClinicalSwitches, setClinicalSwitches } from '../../../shellConfig'

const getShiftBoard = vi.fn()
const listNursingNotes = vi.fn()

vi.mock('../../../api/nurse', () => ({
  getShiftBoard: (...a: unknown[]) => getShiftBoard(...a),
  getOwnCopies: vi.fn(),
  previewNews2: vi.fn(),
  recordObservations: vi.fn(),
  voidObservation: vi.fn(),
  currentUserName: () => 'Nurse A',
}))
vi.mock('../../../api/nursingNotes', async () => {
  const real = await vi.importActual<typeof import('../../../api/nursingNotes')>('../../../api/nursingNotes')
  return { ...real, listNursingNotes: (...a: unknown[]) => listNursingNotes(...a), writeNursingNote: vi.fn() }
})

import NursePanel from './NursePanel'

function card(over: Partial<NursePatientCard> = {}): NursePatientCard {
  return {
    patient_id: 'P001', name: 'Asha Menon', mrn: 'MRN-1', bed: 'GM-07', ward: 'GM', age: 61,
    sex: 'F', diagnosis: 'Pneumonia', department: 'General Medicine', attending_doctor_name: null,
    on_scale_2: false, allergies: [], news2: null,
    last_obs_at: new Date(Date.now() - 130 * 60_000).toISOString(),
    obs_interval_minutes: 240, obs_band: null, obs_response: null,
    obs_due_at: new Date(Date.now() + 20 * 60_000).toISOString(), obs_minutes_until_due: 20,
    obs_due_source: 'ward_interval', obs_due_note: null,
    obs_overdue: false, obs_due_soon: true, medications: [], meds_overdue: 0, meds_due_soon: 0,
    ...over,
  }
}

function board(patients: NursePatientCard[]): ShiftBoard {
  return {
    generated_at: new Date().toISOString(),
    department: 'General Medicine',
    scoring_enabled: false,
    patients,
    totals: {
      patients: patients.length, critical: null, elevated: null, obs_overdue: 0, meds_overdue: 0,
      meds_allergy_blocked: 0,
    },
  }
}

async function openPatients(b: ShiftBoard) {
  getShiftBoard.mockResolvedValue(b)
  render(<NursePanel />)
  await screen.findByText(/patients$/)
  fireEvent.click(screen.getByRole('tab', { name: /My patients/ }))
  await screen.findByRole('button', { name: 'Record obs' })
}

function signIn(role: string) {
  localStorage.setItem('orb_user', JSON.stringify({ id: `U-${role}`, name: role, role }))
}

beforeEach(() => {
  getShiftBoard.mockReset()
  listNursingNotes.mockReset()
  listNursingNotes.mockResolvedValue({ patient_id: 'P001', notes: [] })
  clearOfflineCache()
  act(() => { setClinicalSwitches({ scoring_enabled: false }) })
})

afterEach(() => {
  cleanup()
  resetClinicalSwitches()
})

describe('the Nursing note button', () => {
  it('is offered to a nurse beside Record obs and opens the note sheet', async () => {
    signIn('nurse')
    await openPatients(board([card()]))
    fireEvent.click(screen.getByRole('button', { name: 'Nursing note' }))
    expect(await screen.findByRole('dialog', { name: 'Nursing note' })).toBeTruthy()
    expect(listNursingNotes).toHaveBeenCalledWith('P001')
    expect(screen.getByText('No nursing notes yet for this patient.')).toBeTruthy()
  })

  it('is not offered under the Orb Ward Record profile', async () => {
    signIn('nurse')
    act(() => { setClinicalSwitches({ ward_record: true, scoring_enabled: false, models_enabled: false }) })
    await openPatients(board([card()]))
    expect(screen.queryByRole('button', { name: 'Nursing note' })).toBeNull()
  })

  it('is not offered to a doctor, who reads notes but writes none', async () => {
    signIn('doctor')
    await openPatients(board([card()]))
    expect(screen.queryByRole('button', { name: 'Nursing note' })).toBeNull()
  })
})
