/**
 * ATTACK: one patient's dictated plan landing in another patient's chart.
 *
 * The ward round is the single most time-pressured screen in Orb — roughly 90
 * seconds a bed — so the doctor does not stand still waiting for a transcript.
 * The real sequence at the bedside is: press stop, and WALK. The transcript
 * arrives from the appliance a second or two later, by which time the tablet is
 * already showing the next patient.
 *
 * RoundDictation is not remounted between beds (same element position, only the
 * `patientId` prop changes), so an in-flight transcription survives the move and
 * appends its text through whatever `onDraftChange` closure is current — which
 * belongs to the NEXT patient. The doctor then saves that plan, and one
 * patient's spoken plan is filed into another patient's chart.
 *
 * The same hole has a second mouth: a failed transcription leaves the recorded
 * audio and a "Retry transcription" button on screen, and neither is cleared
 * when the round moves on. Pressing retry at the next bed transcribes the
 * PREVIOUS patient's audio into the current patient's plan.
 */
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'

type Deferred<T> = { promise: Promise<T>; resolve: (v: T) => void; reject: (e: unknown) => void }
function defer<T>(): Deferred<T> {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

const card = (id: string, name: string, bed: string) => ({
  patient_id: id, name, mrn: `MRN${id}`, bed, ward: 'GM', age: 60, sex: 'F',
  diagnosis: 'Pneumonia', department: 'General Medicine', attending_doctor_name: 'Dr Iyer',
  on_scale_2: false, allergies: [], news2: null, last_obs_at: null,
  obs_interval_minutes: 240, obs_band: 'routine', obs_response: 'routine',
  obs_due_at: null, obs_minutes_until_due: null, obs_overdue: false,
  obs_due_soon: false, medications: [], meds_overdue: 0, meds_due_soon: 0,
})

const BOARD = {
  generated_at: '2026-08-07T08:00:00Z',
  department: 'General Medicine',
  patients: [card('P001', 'Anita Rao', 'GM-1'), card('P002', 'Rahul Menon', 'GM-2')],
  totals: { patients: 2, critical: 0, elevated: 0, obs_overdue: 0, meds_overdue: 0 },
}

let transcribePending: Deferred<{ transcript: string; audio_path: string }>
const transcribeAudio = vi.fn(() => {
  transcribePending = defer<{ transcript: string; audio_path: string }>()
  return transcribePending.promise
})
const addClinicalNote = vi.fn(async () => ({ id: 'n1' }))

vi.mock('../../api/nurse', () => ({
  getShiftBoard: vi.fn(async () => BOARD),
}))

vi.mock('../../api/client', () => ({
  getPatients: vi.fn(async () => []),
  getPatientStory: vi.fn(async () => []),
  getSageInsights: vi.fn(async () => ({ insights: [] })),
  getUser: () => ({ id: 'doc-1', name: 'Dr Iyer', role: 'doctor' }),
  addClinicalNote: (...a: unknown[]) => addClinicalNote(...(a as [])),
  transcribeAudio: () => transcribeAudio(),
}))

// Mirrors the real provider, where claimMic/releaseMic are useCallback([]) and
// the context value is memoised. A fresh object per render would be a lie that
// re-fires every consumer effect keyed on these functions.
const SAGE_RUNTIME = {
  posture: null,
  micOwner: null,
  claimMic: () => true,
  releaseMic: () => {},
  cloudSpeechAllowed: false,
}
vi.mock('../../contexts/SageRuntimeContext', () => ({
  useSageRuntime: () => SAGE_RUNTIME,
}))

/** Minimal MediaRecorder: stop() emits one audio chunk then fires onstop, which
 *  is exactly the ordering the real API guarantees. */
class FakeMediaRecorder {
  state = 'inactive'
  ondataavailable: ((e: { data: Blob }) => void) | null = null
  onstop: (() => void) | null = null
  start() { this.state = 'recording' }
  stop() {
    this.state = 'inactive'
    this.ondataavailable?.({ data: new Blob([new Uint8Array(4096)], { type: 'audio/webm' }) })
    this.onstop?.()
  }
}

import RoundPanel from './RoundPanel'

const planBox = () => screen.getByLabelText('Plan for today') as HTMLTextAreaElement

/** The patient the round is standing at — the summary heading, not the rail. */
const currentName = () =>
  (document.querySelector('.round-patient__name') as HTMLElement | null)?.textContent ?? null

async function atBed(name: string) {
  await waitFor(() => expect(currentName()).toBe(name))
}

/** Press record, then stop — leaving a transcription in flight. */
async function dictate() {
  fireEvent.click(await screen.findByRole('button', { name: /Dictate the plan/i }))
  const stop = await screen.findByRole('button', { name: /Stop/i })
  await act(async () => {
    fireEvent.click(stop)
    await Promise.resolve()
  })
}

describe('Ward round dictation across a bed change', () => {
  beforeEach(() => {
    localStorage.clear()
    transcribeAudio.mockClear()
    addClinicalNote.mockClear()
    vi.stubGlobal('MediaRecorder', FakeMediaRecorder)
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }) },
    })
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('never appends the previous bed’s transcript to the next patient’s plan', async () => {
    render(<RoundPanel />)
    await atBed('Anita Rao')

    await dictate()

    // The doctor does not wait for the transcript — they mark the patient seen
    // and walk. Nothing blocks this: the draft is still empty, because the
    // transcript has not landed yet.
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Seen · next bed/i }))
      await Promise.resolve()
    })
    await atBed('Rahul Menon')

    // Anita's transcript arrives while Rahul is on screen.
    await act(async () => {
      transcribePending.resolve({
        transcript: 'Anita Rao — increase furosemide to 40mg BD, repeat U&E tomorrow.',
        audio_path: '/tmp/a.webm',
      })
      await Promise.resolve()
    })

    // It must not be sitting in Rahul's plan box, one tap away from his chart.
    await waitFor(() => {
      expect(planBox().value).not.toMatch(/furosemide/i)
    })
    expect(planBox().value).toBe('')
  })

  it('does not offer to re-transcribe the previous bed’s audio at the next bed', async () => {
    render(<RoundPanel />)
    await atBed('Anita Rao')

    await dictate()

    // Anita's transcription fails, leaving her audio held for a retry.
    await act(async () => {
      transcribePending.reject(new Error('appliance busy'))
      await Promise.resolve()
    })
    expect(await screen.findByText(/Transcription failed/i)).toBeInTheDocument()

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Seen · next bed/i }))
      await Promise.resolve()
    })
    await atBed('Rahul Menon')

    // Anita's failure notice — and the button that would replay HER audio into
    // Rahul's plan — must not have followed the round to the next bed.
    expect(screen.queryByText(/Transcription failed/i)).toBeNull()
    expect(screen.queryByRole('button', { name: /Retry transcription/i })).toBeNull()
    expect(planBox().value).toBe('')
  })

  it('leaves the microphone usable at the next bed while the previous transcript is still running', async () => {
    render(<RoundPanel />)
    await atBed('Anita Rao')

    await dictate()

    // Walk on with Anita's transcription still in flight — the normal case, not
    // the exception: the appliance takes a second or two and the doctor has
    // ninety.
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Seen · next bed/i }))
      await Promise.resolve()
    })
    await atBed('Rahul Menon')

    // Rahul's dictate button must be ready. If the previous bed's transcription
    // still owns this control, the doctor cannot record the patient in front of
    // them — the round stalls at exactly the moment it must not.
    const mic = await screen.findByRole('button', { name: /Dictate the plan/i })
    expect(mic).not.toBeDisabled()
  })
})
