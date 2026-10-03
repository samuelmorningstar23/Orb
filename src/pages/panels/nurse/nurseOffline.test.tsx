/**
 * WHAT A NURSE SEES WHEN THE WI-FI DIES MID-ROUND.
 *
 * Orb's offline mode is READ-ONLY on purpose (see lib/offline/writeGuard): she
 * keeps her board, her obs and her drug round; she cannot chart until she is
 * back. Two failure modes make that dangerous rather than merely inconvenient,
 * and these tests are about both:
 *
 *   1. A cached board that still LOOKS live. She reads a NEWS2 from twenty
 *      minutes ago as if it were current and walks past a deteriorating
 *      patient.
 *   2. A write that looks like it worked. She believes she charted a dose, does
 *      not chart it again, and the record simply never exists.
 */
import { render, screen, act, waitFor } from '@testing-library/react'
import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import type { NursePatientCard, ShiftBoard } from '../../../api/nurse'
import { writeCache, clearOfflineCache } from '../../../lib/offline/phiCache'

const getShiftBoard = vi.fn()
const recordObservations = vi.fn()
const previewNews2 = vi.fn()

vi.mock('../../../api/nurse', () => ({
  getShiftBoard: (...a: unknown[]) => getShiftBoard(...a),
  recordObservations: (...a: unknown[]) => recordObservations(...a),
  previewNews2: (...a: unknown[]) => previewNews2(...a),
  currentUserName: () => 'Nurse A',
}))

import NursePanel from './NursePanel'
import ObsEntrySheet from './ObsEntrySheet'

const NURSE_A = 'NUR001'

/** Sign a nurse in the way api/client stores it — no mock, the real read path. */
function signIn(id: string) {
  localStorage.setItem('orb_user', JSON.stringify({ id, name: 'Nurse A', role: 'nurse' }))
}

function setOnline(value: boolean) {
  Object.defineProperty(navigator, 'onLine', { value, configurable: true })
  act(() => { window.dispatchEvent(new Event(value ? 'online' : 'offline')) })
}

const PATIENT: NursePatientCard = {
  patient_id: 'P001',
  name: 'Asha Menon',
  mrn: 'MRN-1',
  bed: 'GM-4',
  ward: 'GM',
  age: 61,
  sex: 'F',
  diagnosis: 'Community-acquired pneumonia',
  department: 'General Medicine',
  attending_doctor_name: 'Dr Rao',
  on_scale_2: false,
  allergies: [],
  news2: {
    score: 0.5, level: 'elevated', raw_news2: 5, single_param_3: false,
    drivers: ['RR'], breakdown: [], scoring_model: 'NEWS2', spo2_scale: 1,
  },
  last_obs_at: null,
  obs_interval_minutes: 60,
  obs_band: 'Medium',
  obs_response: 'Urgent review',
  obs_due_at: null,
  obs_minutes_until_due: -12,
  obs_overdue: true,
  obs_due_soon: false,
  medications: [],
  meds_overdue: 0,
  meds_due_soon: 0,
}

const BOARD: ShiftBoard = {
  generated_at: new Date().toISOString(),
  department: 'General Medicine',
  patients: [PATIENT],
  totals: { patients: 1, critical: 0, elevated: 1, obs_overdue: 1, meds_overdue: 0 },
}

beforeEach(() => {
  localStorage.clear()
  getShiftBoard.mockReset()
  recordObservations.mockReset()
  previewNews2.mockReset()
  previewNews2.mockResolvedValue({
    news2: BOARD.patients[0].news2,
    schedule: { interval_minutes: 60, band: 'Medium', response: 'Urgent review' },
  })
  signIn(NURSE_A)
  setOnline(true)
})

afterEach(() => {
  // Deliberately does NOT flip the connection back: this runs before React
  // Testing Library's own cleanup, so dispatching 'online' here would fire a
  // state update into a component the test has finished with. beforeEach
  // restores it for the next one.
  clearOfflineCache()
  localStorage.clear()
})

describe('the shift board mid-round', () => {
  it('keeps the patient list when the wi-fi drops, and says how old it is', async () => {
    // She loaded her board 18 minutes ago, before walking into the dead zone.
    writeCache('nurse:board', BOARD, NURSE_A, Date.now() - 18 * 60_000)
    setOnline(false)
    getShiftBoard.mockRejectedValue(new Error('Failed to fetch'))

    await act(async () => { render(<NursePanel />) })

    // The patient is still there — that is the point of the cache.
    expect(await screen.findByText(/Asha Menon/)).toBeInTheDocument()
    // And it is unmistakably labelled.
    const banner = await screen.findByRole('alert')
    expect(banner.textContent).toMatch(/offline/i)
    expect(banner.textContent).toMatch(/18 minutes old/i)
    expect(banner.textContent).toMatch(/nothing can be recorded/i)
  })

  it('says there is nothing saved rather than showing an empty ward', async () => {
    setOnline(false)
    getShiftBoard.mockRejectedValue(new Error('Failed to fetch'))

    await act(async () => { render(<NursePanel />) })

    const banner = await screen.findByRole('alert')
    expect(banner.textContent).toMatch(/no saved copy/i)
    expect(screen.queryByText(/Asha Menon/)).toBeNull()
  })

  it('never serves the previous nurse her patients', async () => {
    writeCache('nurse:board', BOARD, 'NUR999', Date.now() - 60_000)
    setOnline(false)
    getShiftBoard.mockRejectedValue(new Error('Failed to fetch'))

    await act(async () => { render(<NursePanel />) })

    await screen.findByRole('alert')
    expect(screen.queryByText(/Asha Menon/)).toBeNull()
  })

  it('refetches and drops the offline label the moment the wi-fi comes back', async () => {
    writeCache('nurse:board', BOARD, NURSE_A, Date.now() - 18 * 60_000)
    setOnline(false)
    getShiftBoard.mockRejectedValue(new Error('Failed to fetch'))

    await act(async () => { render(<NursePanel />) })
    await screen.findByRole('alert')

    getShiftBoard.mockResolvedValue(BOARD)
    setOnline(true)

    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull())
    expect(screen.getByText(/Asha Menon/)).toBeInTheDocument()
  })

  it('caches the board it fetched, so the NEXT drop is covered', async () => {
    getShiftBoard.mockResolvedValue(BOARD)
    await act(async () => { render(<NursePanel />) })
    await screen.findByText(/Asha Menon/)

    await waitFor(() =>
      expect(localStorage.getItem('orb:offline:nurse:board')).toContain('Asha Menon'))
  })
})

describe('the drug round mid-round', () => {
  /** The same board, with one overdue dose on it. */
  const BOARD_WITH_MED: ShiftBoard = {
    ...BOARD,
    patients: [{
      ...PATIENT,
      medications: [{
        medication_id: 77,
        drug_name: 'Amoxicillin',
        dosage: '500 mg', route: 'PO', frequency: 'TDS',
        schedule_kind: 'scheduled', interval_minutes: 480, status: 'active',
        last_given_at: null, given_count: 0, due_at: null, due_basis: 'order_start',
        missed_slots: 1, minutes_until_due: -40, overdue: true, due_soon: false,
        unverified: false,
      }],
      meds_overdue: 1,
    }],
    totals: { ...BOARD.totals, meds_overdue: 1 },
  }

  it('dates the drug chart INSIDE the round sheet, not just on the board behind it', async () => {
    // The sheet is a full-screen portal on a tablet: it covers the banner that
    // dates the board. A nurse standing at the bed looking at a drug chart she
    // believes is current, deciding whether a dose is late, sees only this
    // screen — so this screen has to say what it is.
    writeCache('nurse:board', BOARD_WITH_MED, NURSE_A, Date.now() - 25 * 60_000)
    setOnline(false)
    getShiftBoard.mockRejectedValue(new Error('Failed to fetch'))

    await act(async () => { render(<NursePanel />) })
    const task = await screen.findByRole('button', { name: /Amoxicillin/ })
    await act(async () => { task.click() })

    const sheet = await screen.findByRole('dialog', { name: /medication round/i })
    expect(sheet.textContent).toMatch(/not live/i)
    expect(sheet.textContent).toMatch(/25 minutes old/i)
    expect(sheet.textContent).toMatch(/nothing can be recorded/i)
  })

  it('says nothing when the round sheet is showing live data', async () => {
    getShiftBoard.mockResolvedValue(BOARD_WITH_MED)

    await act(async () => { render(<NursePanel />) })
    const task = await screen.findByRole('button', { name: /Amoxicillin/ })
    await act(async () => { task.click() })

    const sheet = await screen.findByRole('dialog', { name: /medication round/i })
    expect(sheet.textContent).not.toMatch(/not live/i)
  })
})

describe('writing while offline', () => {
  it('warns BEFORE she fills the obs sheet in, not after she taps Record', async () => {
    // The refusal on submit is correct but late: six values typed at a bedside,
    // then told none of it can be saved. The sheet opens off a cached board, so
    // it already knows.
    writeCache('nurse:board', BOARD, NURSE_A, Date.now() - 9 * 60_000)
    setOnline(false)
    getShiftBoard.mockRejectedValue(new Error('Failed to fetch'))

    await act(async () => { render(<NursePanel />) })
    const patientsTab = await screen.findByRole('tab', { name: /My patients/ })
    await act(async () => { patientsTab.click() })
    const record = await screen.findByRole('button', { name: /^Record obs$/ })
    await act(async () => { record.click() })

    const sheet = await screen.findByRole('dialog', { name: /record observations/i })
    expect(sheet.textContent).toMatch(/not live/i)
    expect(sheet.textContent).toMatch(/nothing can be recorded/i)
  })

  it('refuses to record observations — and never reaches the server', async () => {
    setOnline(false)
    localStorage.setItem('orb:nurse:obs-draft:P001', JSON.stringify({
      values: {
        respiratory_rate: '22', spo2: '95', systolic_bp: '118',
        diastolic_bp: '76', heart_rate: '96', temperature: '37.4',
      },
      consciousness: 'alert',
      onOxygen: false,
      savedAt: Date.now(),
      ownerId: NURSE_A,
    }))

    render(<ObsEntrySheet patient={PATIENT} onClose={() => {}} onRecorded={() => {}} />)

    const button = await screen.findByRole('button', { name: /record observations/i })
    await act(async () => { button.click() })

    // Nothing was sent, and nothing was queued to be sent later.
    expect(recordObservations).not.toHaveBeenCalled()
    const message = (await screen.findByText(/has not been saved/i)).textContent ?? ''
    expect(message).toMatch(/offline/i)
    // It must never promise a later sync.
    expect(message).not.toMatch(/queue|sync|when you.?re back/i)
  })

  it('still records normally once back online (the guard is not a latch)', async () => {
    setOnline(false)
    localStorage.setItem('orb:nurse:obs-draft:P001', JSON.stringify({
      values: {
        respiratory_rate: '22', spo2: '95', systolic_bp: '118',
        diastolic_bp: '76', heart_rate: '96', temperature: '37.4',
      },
      consciousness: 'alert',
      onOxygen: false,
      savedAt: Date.now(),
      ownerId: NURSE_A,
    }))
    recordObservations.mockResolvedValue({
      vitals_id: 1,
      recorded_at: new Date().toISOString(),
      news2: PATIENT.news2,
      schedule: { interval_minutes: 60, band: 'Medium', response: 'Urgent review' },
      replayed: false,
    })

    render(<ObsEntrySheet patient={PATIENT} onClose={() => {}} onRecorded={() => {}} />)
    const button = await screen.findByRole('button', { name: /record observations/i })
    await act(async () => { button.click() })
    expect(recordObservations).not.toHaveBeenCalled()

    setOnline(true)
    await act(async () => { button.click() })
    await waitFor(() => expect(recordObservations).toHaveBeenCalledTimes(1))
  })
})
