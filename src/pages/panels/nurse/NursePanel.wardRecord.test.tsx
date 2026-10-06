/**
 * The nurse shift board with scoring switched off (the Orb Ward Record
 * profile, 6 October 2026).
 *
 * The server sends news2 null, obs_band and obs_response null, the band
 * totals null, and the due time from the ward's fixed interval
 * (ward_observation_settings). A ward with no interval sends no due time and
 * "Set the ward's observation interval". The board must draw no NEWS2 badge,
 * no "NEWS2 not recorded", no critical or elevated chip (not even a 0), and
 * show the ward interval in place of the band.
 */
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act } from 'react'
import type { NursePatientCard, ShiftBoard } from '../../../api/nurse'
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

function scoringOffBoard(patients: NursePatientCard[]): ShiftBoard {
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

async function renderPanel(b: ShiftBoard) {
  getShiftBoard.mockResolvedValue(b)
  render(<NursePanel />)
  await screen.findByText(/patients$/)
}

beforeEach(() => {
  getShiftBoard.mockReset()
  clearOfflineCache()
  localStorage.setItem('orb_user', JSON.stringify({ id: 'NUR001', name: 'Nurse A', role: 'nurse' }))
  act(() => { setClinicalSwitches({ scoring_enabled: false }) })
})

afterEach(() => {
  cleanup()
  resetClinicalSwitches()
})

describe('the shift board with scoring switched off', () => {
  it('shows no critical or elevated chip, not even a 0', async () => {
    await renderPanel(scoringOffBoard([card()]))
    const totals = document.querySelector('.nurse-totals') as HTMLElement
    expect(totals.textContent).not.toMatch(/critical|elevated/)
    expect(totals.textContent).toMatch(/obs overdue/)
  })

  it('says the due times follow the ward interval, not the NEWS2 table', async () => {
    await renderPanel(scoringOffBoard([card()]))
    const note = document.querySelector('.nurse-footnote') as HTMLElement
    expect(note.textContent).toMatch(/ward's fixed interval/)
    expect(note.textContent).not.toMatch(/NEWS2/)
  })

  it('the observation task shows the ward interval, never a band, and its label no score', async () => {
    await renderPanel(scoringOffBoard([card()]))
    const row = screen.getByRole('button', { name: /record observations/ })
    expect(row.textContent).toContain('Ward interval, every 4 hours')
    expect(row.getAttribute('aria-label')).not.toMatch(/NEWS2/)
    expect(row.querySelector('.news2')).toBeNull()
  })

  it('the patient card draws no badge, no response line and says the next round', async () => {
    await renderPanel(scoringOffBoard([card()]))
    fireEvent.click(screen.getByRole('tab', { name: /My patients/ }))
    const tile = document.querySelector('.patient-card') as HTMLElement
    expect(tile.querySelector('.news2')).toBeNull()
    expect(tile.querySelector('.patient-card__response')).toBeNull()
    expect(tile.textContent).not.toMatch(/NEWS2|—/)
    expect(tile.textContent).toMatch(/next obs/)
  })

  it('a ward with no interval says to set one and shows no due time', async () => {
    const none = card({
      obs_interval_minutes: null, obs_due_at: null, obs_minutes_until_due: null,
      obs_due_source: null, obs_due_note: "Set the ward's observation interval", obs_due_soon: false,
    })
    await renderPanel(scoringOffBoard([none]))
    // Nothing is due, so the queue lists no observation task for her.
    expect(screen.queryByRole('button', { name: /record observations/ })).toBeNull()
    fireEvent.click(screen.getByRole('tab', { name: /My patients/ }))
    expect(screen.getByText("Set the ward's observation interval")).toBeTruthy()
    expect(document.querySelector('.patient-card .is-late')).toBeNull()
  })
})
