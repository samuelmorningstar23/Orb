/**
 * The observation sheet in the Orb Ward Record (6 October 2026).
 *
 *   The ward clerk (vitals.transcribe, no vitals.write) copies a round from
 *   the paper sheet: a required "Time on the sheet" becomes taken_at, an
 *   "Illegible" tick blanks a reading and names it, and no score is shown or
 *   even asked for. With scoring switched off on this computer nobody sees a
 *   score. A nurse with scoring on sees exactly what she saw before.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const recordObservations = vi.fn()
const previewNews2 = vi.fn()

vi.mock('../../../api/nurse', () => ({
  recordObservations: (...args: unknown[]) => recordObservations(...args),
  previewNews2: (...args: unknown[]) => previewNews2(...args),
}))

import ObsEntrySheet from './ObsEntrySheet'
import { resetClinicalSwitches, setClinicalSwitches } from '../../../shellConfig'

function signIn(role: string) {
  localStorage.setItem('orb_user', JSON.stringify({ id: `U-${role}`, name: role, role }))
}

const PATIENT = { patient_id: 'P900', name: 'Sheet Patient', bed: '7', mrn: 'MRN9' }
const NEWS2 = { score: 0.1, raw_news2: 1, level: 'stable', drivers: [], breakdown: [] }
const SCHEDULE = { band: 'Low', response: 'Routine observations' }

function pad(n: number) { return String(n).padStart(2, '0') }
function localInput(d: Date) {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

beforeEach(() => {
  localStorage.clear()
  recordObservations.mockReset()
  previewNews2.mockReset()
  previewNews2.mockResolvedValue({ news2: NEWS2, schedule: SCHEDULE })
  recordObservations.mockResolvedValue({
    vitals_id: 1, recorded_at: 'x', news2: null, schedule: null, replayed: false,
  })
})

afterEach(() => {
  cleanup()
  resetClinicalSwitches()
})

/** A reading's input, by its field key (the label also names its Illegible tick). */
function field(key: string): HTMLInputElement {
  return document.getElementById(`obs-${key}`) as HTMLInputElement
}

function renderSheet() {
  const onRecorded = vi.fn()
  render(<ObsEntrySheet patient={PATIENT} onClose={vi.fn()} onRecorded={onRecorded} />)
  return { onRecorded }
}

describe('the ward clerk copies the paper sheet', () => {
  beforeEach(() => signIn('ward_clerk'))

  it('asks for the time on the sheet and sends it as taken_at, with an illegible SpO2', async () => {
    const { onRecorded } = renderSheet()
    expect(screen.getByRole('heading', { name: 'Copy observations from the sheet' })).toBeTruthy()
    const when = new Date(Date.now() - 2 * 24 * 3600 * 1000)
    when.setSeconds(0, 0)
    fireEvent.change(screen.getByLabelText('Time on the sheet'), { target: { value: localInput(when) } })
    fireEvent.change(field('heart_rate'), { target: { value: '82' } })
    fireEvent.change(field('spo2'), { target: { value: '96' } })
    fireEvent.click(screen.getByLabelText('Oxygen saturation illegible on the sheet'))
    expect(field('spo2').value).toBe('')
    expect(field('spo2').disabled).toBe(true)

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Record observations' }))
    })
    expect(recordObservations).toHaveBeenCalledTimes(1)
    const [pid, body] = recordObservations.mock.calls[0]
    expect(pid).toBe('P900')
    expect(body.taken_at).toBe(when.toISOString())
    expect(body.heart_rate).toBe(82)
    expect(body.spo2).toBeNull()
    expect(body.illegible).toEqual(['spo2'])
    await waitFor(() => expect(onRecorded).toHaveBeenCalledWith(null, null))
  })

  it('shows no live score and never asks for one', async () => {
    renderSheet()
    fireEvent.change(field('respiratory_rate'), { target: { value: '30' } })
    await new Promise(r => setTimeout(r, 400))
    expect(previewNews2).not.toHaveBeenCalled()
    expect(screen.queryByText(/NEWS2/)).toBeNull()
    expect(screen.queryByText('Routine observations')).toBeNull()
    expect(screen.getByText(/No score is shown/)).toBeTruthy()
  })

  it('refuses a sheet time in the future', () => {
    renderSheet()
    const later = new Date(Date.now() + 3 * 3600 * 1000)
    fireEvent.change(screen.getByLabelText('Time on the sheet'), { target: { value: localInput(later) } })
    fireEvent.change(field('heart_rate'), { target: { value: '82' } })
    expect(screen.getByText('The time on the sheet cannot be in the future')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Record observations' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('an illegible ACVPU is sent as illegible and its buttons are locked', () => {
    renderSheet()
    fireEvent.click(screen.getByLabelText('Consciousness illegible on the sheet'))
    expect((screen.getByRole('button', { name: /Voice/ }) as HTMLButtonElement).disabled).toBe(true)
    // An all-illegible round is still a round: Record is offered.
    expect((screen.getByRole('button', { name: 'Record observations' }) as HTMLButtonElement).disabled).toBe(false)
  })
})

describe('a nurse at the bedside', () => {
  beforeEach(() => signIn('nurse'))

  it('sees no score and no response line while scoring is switched off', async () => {
    act(() => { setClinicalSwitches({ scoring_enabled: false }) })
    const { onRecorded } = renderSheet()
    expect(screen.queryByLabelText('Time on the sheet')).toBeNull()
    expect(screen.queryByText('Illegible')).toBeNull()
    fireEvent.change(field('heart_rate'), { target: { value: '82' } })
    await new Promise(r => setTimeout(r, 400))
    expect(previewNews2).not.toHaveBeenCalled()
    expect(screen.getByText('Scoring is switched off on this computer. No score is shown.')).toBeTruthy()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Record observations' }))
    })
    await waitFor(() => expect(onRecorded).toHaveBeenCalledWith(null, null))
  })

  it('still sees the live score with scoring on, and no sheet time or illegible ticks', async () => {
    renderSheet()
    expect(screen.queryByLabelText('Time on the sheet')).toBeNull()
    expect(screen.queryByText('Illegible')).toBeNull()
    fireEvent.change(field('heart_rate'), { target: { value: '82' } })
    await waitFor(() => expect(previewNews2).toHaveBeenCalled())
    expect(await screen.findByText('Routine observations')).toBeTruthy()
  })
})
