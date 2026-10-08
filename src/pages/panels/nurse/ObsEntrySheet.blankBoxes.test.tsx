/**
 * Nothing preselected on the observation sheet (Orb Ward Record, 9 October 2026).
 *
 * A paper sheet can leave the consciousness (ACVPU) box or the air or oxygen
 * box empty, and the rounds file counts that as blank. The sheet used to
 * preselect Alert and On air, so an empty box was sent as alert and air and
 * stored as a finding nobody made. Now both start unselected:
 *
 *   * a nurse at the bedside must choose both before Record is offered;
 *   * the clerk also gets "Not recorded on the sheet", and an empty box is
 *     sent as null whether she picks that or leaves it untouched;
 *   * a draft saved before this change does not bring back the old
 *     preselection as if it were a choice.
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
import { obsDraftKey } from '../../../lib/nurseDrafts'
import { resetClinicalSwitches } from '../../../shellConfig'

function signIn(role: string) {
  localStorage.setItem('orb_user', JSON.stringify({ id: `U-${role}`, name: role, role }))
}

const PATIENT = { patient_id: 'P910', name: 'Blank Box Patient', bed: '3', mrn: 'MRN10' }
const NEWS2 = { score: 0.1, raw_news2: 0, level: 'stable', drivers: [], breakdown: [] }
const SCHEDULE = { band: 'Low', response: 'Routine observations' }

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

function recordButton(): HTMLButtonElement {
  return screen.getByRole('button', { name: 'Record observations' }) as HTMLButtonElement
}

function pressed(): string[] {
  return screen.getAllByRole('button')
    .filter(b => b.getAttribute('aria-pressed') === 'true')
    .map(b => b.textContent ?? '')
}

function renderSheet() {
  render(<ObsEntrySheet patient={PATIENT} onClose={vi.fn()} onRecorded={vi.fn()} />)
}

describe('a nurse at the bedside', () => {
  beforeEach(() => signIn('nurse'))

  it('sees no consciousness or oxygen choice preselected', () => {
    renderSheet()
    expect(pressed()).toEqual([])
    expect(screen.getAllByText('Choose one')).toHaveLength(2)
    expect(screen.queryByText('Not recorded on the sheet')).toBeNull()
  })

  it('cannot record until she has chosen both, and then sends what she chose', async () => {
    renderSheet()
    fireEvent.change(field('heart_rate'), { target: { value: '82' } })
    expect(recordButton().disabled).toBe(true)
    expect(recordButton().title).toBe('Choose the level of consciousness and air or oxygen')

    fireEvent.click(screen.getByRole('button', { name: /Confusion/ }))
    expect(recordButton().disabled).toBe(true)
    expect(screen.getAllByText('Choose one')).toHaveLength(1)

    fireEvent.click(screen.getByRole('button', { name: 'On oxygen' }))
    expect(recordButton().disabled).toBe(false)
    expect(screen.queryByText('Choose one')).toBeNull()

    await act(async () => { fireEvent.click(recordButton()) })
    expect(recordObservations).toHaveBeenCalledTimes(1)
    const body = recordObservations.mock.calls[0][1]
    expect(body.consciousness).toBe('confusion')
    expect(body.on_oxygen).toBe(true)
  })

  it('previews a half chosen set with both boxes empty, never as alert and air', async () => {
    renderSheet()
    fireEvent.change(field('respiratory_rate'), { target: { value: '24' } })
    await act(async () => { await new Promise(r => setTimeout(r, 300)) })
    expect(previewNews2).toHaveBeenCalled()
    const body = previewNews2.mock.calls.at(-1)?.[1]
    expect(body.consciousness).toBeNull()
    expect(body.on_oxygen).toBeNull()
  })

  it('does not bring back an older draft\'s alert and on air as a choice', () => {
    localStorage.setItem(obsDraftKey('P910'), JSON.stringify({
      values: { respiratory_rate: '18', spo2: '', systolic_bp: '', diastolic_bp: '',
        heart_rate: '', temperature: '' },
      consciousness: 'alert',
      onOxygen: false,
      savedAt: Date.now(),
      ownerId: 'U-nurse',
    }))
    renderSheet()
    expect(field('respiratory_rate').value).toBe('18')
    expect(pressed()).toEqual([])
    expect(recordButton().disabled).toBe(true)
  })
})

describe('the ward clerk copying the sheet', () => {
  beforeEach(() => signIn('ward_clerk'))

  it('starts unselected and sends an untouched box as empty', async () => {
    renderSheet()
    expect(pressed()).toEqual([])
    expect(screen.queryByText('Choose one')).toBeNull()
    fireEvent.change(field('heart_rate'), { target: { value: '90' } })
    expect(recordButton().disabled).toBe(false)
    await act(async () => { fireEvent.click(recordButton()) })
    const body = recordObservations.mock.calls[0][1]
    expect(body.consciousness).toBeNull()
    expect(body.on_oxygen).toBeNull()
    expect(body.illegible).toBeUndefined()
  })

  it('can say a box was not recorded on the sheet, which is sent as empty', async () => {
    renderSheet()
    const notRecorded = screen.getAllByRole('button', { name: 'Not recorded on the sheet' })
    expect(notRecorded).toHaveLength(2)
    notRecorded.forEach(button => fireEvent.click(button))
    expect(pressed()).toEqual(['Not recorded on the sheet', 'Not recorded on the sheet'])
    // "Not recorded" is a choice, not a reading: nothing to record yet.
    expect(recordButton().disabled).toBe(true)
    fireEvent.change(field('temperature'), { target: { value: '37.2' } })
    await act(async () => { fireEvent.click(recordButton()) })
    const body = recordObservations.mock.calls[0][1]
    expect(body.consciousness).toBeNull()
    expect(body.on_oxygen).toBeNull()
    expect(body.temperature).toBe(37.2)
  })

  it('still sends a level and air when she copies them', async () => {
    renderSheet()
    fireEvent.click(screen.getByRole('button', { name: /Alert/ }))
    fireEvent.click(screen.getByRole('button', { name: 'On air' }))
    await act(async () => { fireEvent.click(recordButton()) })
    const body = recordObservations.mock.calls[0][1]
    expect(body.consciousness).toBe('alert')
    expect(body.on_oxygen).toBe(false)
  })
})
