/**
 * Today with scoring switched off (the Orb Ward Record profile, 6 October
 * 2026). The nurse board the page composes from then carries news2 null for
 * every patient and scoring_enabled false. The "Highest NEWS2 scores" section
 * must not appear at all: its empty state says "No patient is scoring above
 * threshold" and "All N patients are at NEWS2 4 or below", which nobody
 * worked out on this computer.
 */
import { render, screen, act, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetClinicalSwitches, setClinicalSwitches } from '../../shellConfig'

const WARD = 'General Medicine'

function card(id: string, name: string) {
  return {
    patient_id: id, name, mrn: `MRN-${id}`, bed: `GM-${id}`, ward: WARD, age: 61, sex: 'F',
    diagnosis: 'Pneumonia', department: WARD, attending_doctor_name: 'Dr Iyer',
    on_scale_2: false, allergies: [], news2: null,
    last_obs_at: '2026-10-06T08:40:00+00:00', obs_interval_minutes: 240,
    obs_band: null, obs_response: null, obs_due_at: null, obs_due_source: 'ward_interval',
    obs_due_note: null, obs_minutes_until_due: 40, obs_overdue: false, obs_due_soon: false,
    medications: [], meds_overdue: 0, meds_due_soon: 0,
  }
}

function board(scoring: boolean) {
  const patients = [card('01', 'Anita Rao'), card('02', 'Vikas Shah')]
  return {
    generated_at: new Date().toISOString(), department: WARD, scoring_enabled: scoring, patients,
    totals: { patients: 2, critical: scoring ? 0 : null, elevated: scoring ? 0 : null, obs_overdue: 0, meds_overdue: 0 },
  }
}

let currentBoard: unknown = board(false)

function stubFetch() {
  const routes: Record<string, unknown> = {
    '/nurse/board': null, '/relay/alerts': [], '/scribe/notes': [], '/chart/unseen': { counts: {} },
    '/outcome/shift-status': { generated_at: new Date().toISOString(), status_available: false, shifts: [] },
  }
  vi.stubGlobal('fetch', vi.fn(async (url: unknown) => {
    const u = String(url)
    const key = Object.keys(routes).find(k => u.includes(k))
    const body = key === '/nurse/board' ? currentBoard : key ? routes[key] : undefined
    if (body === undefined) {
      return { ok: false, status: 500, headers: { get: () => null }, json: async () => ({ detail: 'no route' }) }
    }
    return { ok: true, status: 200, headers: { get: () => null }, json: async () => body }
  }))
}

import DoctorHomePanel from './DoctorHomePanel'

async function home() {
  localStorage.setItem('orb_user', JSON.stringify({ id: 'DOC001', name: 'Dr Iyer', role: 'doctor', department: WARD }))
  render(<DoctorHomePanel />)
  await screen.findByRole('heading', { name: 'Today', level: 1 })
  await act(async () => { await Promise.resolve(); await Promise.resolve() })
}

beforeEach(() => {
  localStorage.clear()
  stubFetch()
})

afterEach(() => {
  cleanup()
  resetClinicalSwitches()
  vi.unstubAllGlobals()
})

describe('Today with scoring switched off', () => {
  it('has no NEWS2 section and makes no claim about who is scoring', async () => {
    currentBoard = board(false)
    act(() => { setClinicalSwitches({ scoring_enabled: false }) })
    await home()
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    expect(screen.queryByRole('heading', { name: /Highest NEWS2 scores/ })).toBeNull()
    expect(document.body.textContent).not.toMatch(/scoring above threshold|NEWS2 4 or below/)
    expect(document.querySelector('.news2')).toBeNull()
  })

  it('keeps the section with scoring on', async () => {
    currentBoard = board(true)
    await home()
    expect(await screen.findByRole('heading', { name: /Highest NEWS2 scores/ })).toBeTruthy()
  })
})
