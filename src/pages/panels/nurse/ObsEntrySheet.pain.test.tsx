/**
 * The pain score on the observation sheet (21 October 2026, founding ward
 * item 1): the sheet's last box, a whole number from 0 to 10, sent with the
 * set and never scored. A blank box is sent as null; the clerk can tick it
 * illegible; a value off the scale or a half point is refused before save.
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const recordObservations = vi.fn()
const previewNews2 = vi.fn()

vi.mock('../../../api/nurse', () => ({
  recordObservations: (...args: unknown[]) => recordObservations(...args),
  previewNews2: (...args: unknown[]) => previewNews2(...args),
}))

import ObsEntrySheet from './ObsEntrySheet'
import { resetClinicalSwitches, setClinicalSwitches } from '../../../shellConfig'
import { obsDraftKey } from '../../../lib/nurseDrafts'

function signIn(role: string) {
  localStorage.setItem('orb_user', JSON.stringify({ id: `U-${role}`, name: role, role }))
}

const PATIENT = { patient_id: 'P901', name: 'Pain Patient', bed: '3', mrn: 'MRN3' }
const NEWS2 = {
  score: 0.1, raw_news2: 1, level: 'stable', drivers: [],
  breakdown: [{ parameter: 'Heart rate', value: 82, points: 0 }],
}
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

function field(key: string): HTMLInputElement {
  return document.getElementById(`obs-${key}`) as HTMLInputElement
}

function renderSheet() {
  const onRecorded = vi.fn()
  render(<ObsEntrySheet patient={PATIENT} onClose={vi.fn()} onRecorded={onRecorded} />)
  return { onRecorded }
}

function chooseBedsideBoxes() {
  fireEvent.click(screen.getByRole('button', { name: /Alert/ }))
  fireEvent.click(screen.getByRole('button', { name: 'On air' }))
}

describe('a nurse at the bedside', () => {
  beforeEach(() => { signIn('nurse'); act(() => { setClinicalSwitches({ scoring_enabled: false }) }) })

  it('sends the pain score with the set as a number', async () => {
    renderSheet()
    fireEvent.change(field('heart_rate'), { target: { value: '82' } })
    fireEvent.change(field('pain_score'), { target: { value: '7' } })
    chooseBedsideBoxes()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Record observations' }))
    })
    expect(recordObservations).toHaveBeenCalledTimes(1)
    const body = recordObservations.mock.calls[0][1]
    expect(body.pain_score).toBe(7)
    expect(body.heart_rate).toBe(82)
  })

  it('sends a blank pain box as null, so the record counts it as blank', async () => {
    renderSheet()
    fireEvent.change(field('heart_rate'), { target: { value: '82' } })
    chooseBedsideBoxes()
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Record observations' }))
    })
    expect(recordObservations.mock.calls[0][1].pain_score).toBeNull()
  })

  it('refuses a score off the scale or a half point before save', () => {
    renderSheet()
    fireEvent.change(field('heart_rate'), { target: { value: '82' } })
    chooseBedsideBoxes()
    const record = () => screen.getByRole('button', { name: 'Record observations' }) as HTMLButtonElement

    fireEvent.change(field('pain_score'), { target: { value: '11' } })
    expect(screen.getByText('A whole number from 0 to 10')).toBeTruthy()
    expect(record().disabled).toBe(true)

    fireEvent.change(field('pain_score'), { target: { value: '7.5' } })
    expect(screen.getByText('A whole number from 0 to 10')).toBeTruthy()
    expect(record().disabled).toBe(true)

    fireEvent.change(field('pain_score'), { target: { value: '0' } })
    expect(screen.queryByText('A whole number from 0 to 10')).toBeNull()
    expect(record().disabled).toBe(false)
  })

  it('shows no NEWS2 points chip beside the pain score, with scoring on', async () => {
    act(() => { setClinicalSwitches({ scoring_enabled: true }) })
    renderSheet()
    fireEvent.change(field('heart_rate'), { target: { value: '82' } })
    fireEvent.change(field('pain_score'), { target: { value: '9' } })
    await act(async () => { await new Promise(r => setTimeout(r, 400)) })
    expect(previewNews2).toHaveBeenCalled()
    const painField = field('pain_score').closest('.obs-field') as HTMLElement
    expect(painField.querySelector('.obs-field__points')).toBeNull()
    expect(painField.textContent).toContain('Pain score')
    expect(painField.textContent).toContain('0 to 10')
  })
})

describe('the ward clerk copies the paper sheet', () => {
  beforeEach(() => signIn('ward_clerk'))

  it('copies the pain score and can tick it illegible', async () => {
    renderSheet()
    const when = new Date(Date.now() - 3 * 3600 * 1000)
    when.setSeconds(0, 0)
    fireEvent.change(screen.getByLabelText('Time on the sheet'), { target: { value: localInput(when) } })
    fireEvent.change(field('heart_rate'), { target: { value: '82' } })
    fireEvent.change(field('pain_score'), { target: { value: '4' } })
    fireEvent.click(screen.getByLabelText('Pain score illegible on the sheet'))
    expect(field('pain_score').value).toBe('')
    expect(field('pain_score').disabled).toBe(true)

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Record observations' }))
    })
    expect(recordObservations).toHaveBeenCalledTimes(1)
    const body = recordObservations.mock.calls[0][1]
    expect(body.pain_score).toBeNull()
    expect(body.illegible).toEqual(['pain_score'])
  })

  it('an older draft without the pain box is restored with it blank', async () => {
    localStorage.setItem(obsDraftKey(PATIENT.patient_id), JSON.stringify({
      values: { respiratory_rate: '18', spo2: '', systolic_bp: '', diastolic_bp: '', heart_rate: '', temperature: '' },
      savedAt: Date.now(), ownerId: 'U-ward_clerk', choices: 2, consciousness: null, oxygen: null,
    }))
    renderSheet()
    expect(await screen.findByText(/were restored/i)).toBeTruthy()
    expect(field('respiratory_rate').value).toBe('18')
    expect(field('pain_score').value).toBe('')
  })
})
