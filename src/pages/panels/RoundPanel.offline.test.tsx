/**
 * WARD ROUND — a plan typed with no connection must not be taken.
 *
 * The round panel used to call addClinicalNote regardless of connectivity and
 * label the result "queued" when offline. That label was honest about the queue
 * and silent about the queue's life expectancy: api/client drops the whole
 * queue at sign-out, and a round is exactly the workflow where sign-out follows
 * quickly (the doctor finishes the bay and hands the tablet on).
 *
 * A plan is also the highest-value free text on a ward round — it is what the
 * next shift reads. Losing one silently is worse than refusing to take it, so
 * the panel now refuses while offline and keeps the doctor's text in the box.
 *
 * See offlineWriteRefusal.test.tsx for the same fix on Relay, and for the test
 * that demonstrates the queue being destroyed.
 */
import { render, screen, waitFor, act, fireEvent, cleanup } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'

const card = (id: string, name: string, bed: string) => ({
  patient_id: id, name, mrn: `MRN-${id}`, bed, ward: 'GM', age: 62, sex: 'M',
  diagnosis: 'Community-acquired pneumonia', department: 'General Medicine',
  attending_doctor_name: 'Dr Iyer', on_scale_2: false, allergies: [], news2: null,
  last_obs_at: '2026-08-07T06:00:00Z', obs_interval_minutes: 240, obs_band: 'routine',
  obs_response: 'routine', obs_due_at: null, obs_minutes_until_due: 120,
  obs_overdue: false, obs_due_soon: false, medications: [], meds_overdue: 0,
  meds_due_soon: 0,
})

const BOARD = {
  generated_at: '2026-08-07T08:00:00Z',
  department: 'General Medicine',
  patients: [card('P002', 'Anita Rao', 'GM-2')],
  totals: { patients: 1, critical: 0, elevated: 0, obs_overdue: 0, meds_overdue: 0 },
}

const addClinicalNote = vi.fn(async () => ({ id: 'n1' }))

vi.mock('../../api/nurse', () => ({ getShiftBoard: async () => BOARD }))
vi.mock('../../api/client', () => ({
  getPatients: async () => [],
  getPatientStory: async () => [],
  getSageInsights: async () => ({ insights: [] }),
  getUser: () => ({ id: 'doc-1', name: 'Dr Iyer', role: 'doctor' }),
  addClinicalNote: (...a: unknown[]) => addClinicalNote(...(a as [])),
  transcribeAudio: vi.fn(async () => ({ transcript: '', audio_path: '' })),
}))

const SAGE_RUNTIME = {
  posture: null, micOwner: null,
  claimMic: () => true, releaseMic: () => {},
}
vi.mock('../../contexts/SageRuntimeContext', () => ({ useSageRuntime: () => SAGE_RUNTIME }))

import RoundPanel from './RoundPanel'

function setOnline(value: boolean) {
  Object.defineProperty(navigator, 'onLine', { value, configurable: true })
}

describe('Ward round, with no connection', () => {
  beforeEach(() => {
    localStorage.clear()
    addClinicalNote.mockClear()
  })
  afterEach(() => { cleanup(); setOnline(true) })

  it('refuses to save the plan rather than queuing it as "queued"', async () => {
    setOnline(false)
    render(<RoundPanel />)

    await waitFor(() =>
      expect(document.querySelector('.round-patient__name')?.textContent).toBe('Anita Rao'),
    )

    const box = document.querySelector('.round-dictate__text') as HTMLTextAreaElement
    expect(box).toBeTruthy()

    await act(async () => {
      fireEvent.change(box, { target: { value: 'Continue IV co-amoxiclav, review CXR in AM.' } })
      await Promise.resolve()
    })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /save plan to chart/i }))
      await Promise.resolve()
    })

    // Never reached the client, so nothing was queued and nothing can be lost.
    expect(addClinicalNote).not.toHaveBeenCalled()

    // No "queued" reassurance anywhere on screen.
    expect(document.querySelector('.round-dictate__queued')).toBeNull()

    // The doctor is told plainly, and her text is still in the box.
    const err = document.querySelector('.round-dictate__error-inline')
    expect(err?.textContent ?? '').toMatch(/offline/i)
    expect(err?.textContent ?? '').toMatch(/has not been saved/i)
    expect(box.value).toContain('co-amoxiclav')
  })
})
