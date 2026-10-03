/**
 * Two clinical-record properties of the bedside observation sheet, on a SHARED
 * WARD TABLET:
 *
 *   1. one tap or ten, exactly one observation set is charted;
 *   2. an in-progress draft belongs to the nurse who typed it and to nobody
 *      else — while still surviving an interruption WITHIN that nurse's session,
 *      which is the whole point of the draft.
 *
 * Both were broken. The double-tap guard was `saving` state, which both taps of
 * a real double-tap read as false, and `taken_at` was re-stamped per submit so
 * the server's exact-timestamp replay guard could never fire. The draft was
 * keyed by patient alone and outlived logout.
 */
import { render, screen, waitFor, act, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { NursePatientCard } from '../../../api/nurse'

const recordObservations = vi.fn()
const previewNews2 = vi.fn()

vi.mock('../../../api/nurse', () => ({
  recordObservations: (...args: unknown[]) => recordObservations(...args),
  previewNews2: (...args: unknown[]) => previewNews2(...args),
}))

/** Sign a nurse in the way api/client stores it — no mock, the real read path. */
function signIn(id: string) {
  localStorage.setItem('orb_user', JSON.stringify({ id, name: 'Nurse', role: 'nurse' }))
}

import ObsEntrySheet from './ObsEntrySheet'
import { obsDraftKey, clearNurseDrafts, NURSE_DRAFT_MAX_AGE_MS } from '../../../lib/nurseDrafts'

const PATIENT = {
  patient_id: 'P001',
  name: 'Test Patient',
  bed: '4A',
  mrn: 'MRN1',
  allergies: [],
  on_scale_2: false,
} as unknown as NursePatientCard

const NEWS2 = { score: 1, raw_news2: 1, level: 'low', drivers: [], breakdown: [] }
const SCHEDULE = { band: 'low', response: 'routine' }

function writeDraft(overrides: Record<string, unknown> = {}) {
  localStorage.setItem(obsDraftKey('P001'), JSON.stringify({
    values: {
      respiratory_rate: '18', spo2: '97', systolic_bp: '120',
      diastolic_bp: '80', heart_rate: '72', temperature: '36.8',
    },
    consciousness: 'alert',
    onOxygen: false,
    savedAt: Date.now(),
    ownerId: 'NUR001',
    ...overrides,
  }))
}

beforeEach(() => {
  localStorage.clear()
  recordObservations.mockReset()
  previewNews2.mockReset()
  previewNews2.mockResolvedValue({ news2: NEWS2, schedule: SCHEDULE })
  signIn('NUR001')
})

function renderSheet() {
  const onRecorded = vi.fn()
  const onClose = vi.fn()
  render(<ObsEntrySheet patient={PATIENT} onClose={onClose} onRecorded={onRecorded} />)
  return { onRecorded, onClose }
}

describe('double-tap cannot chart two observation sets', () => {
  it('sends exactly one request when the button is tapped twice in the same tick', async () => {
    let resolve!: (v: unknown) => void
    recordObservations.mockImplementation(() => new Promise(r => { resolve = r }))
    writeDraft()
    renderSheet()

    const button = await screen.findByRole('button', { name: /record observations/i })
    // Two taps inside ONE act(): this is the 6 ms double-tap. Both handlers run
    // against the render where `saving` is still false, which is exactly why a
    // state-based guard did not hold.
    await act(async () => {
      button.click()
      button.click()
    })

    expect(recordObservations).toHaveBeenCalledTimes(1)

    await act(async () => {
      resolve({ news2: NEWS2, schedule: SCHEDULE })
    })
  })

  it('re-uses one taken_at across submits, so the server replay guard can fire', async () => {
    recordObservations.mockRejectedValueOnce(new Error('network'))
    recordObservations.mockResolvedValueOnce({ news2: NEWS2, schedule: SCHEDULE })
    writeDraft()
    renderSheet()

    const button = await screen.findByRole('button', { name: /record observations/i })
    await act(async () => { button.click() })
    await screen.findByText(/network/i)
    await act(async () => { button.click() })

    await waitFor(() => expect(recordObservations).toHaveBeenCalledTimes(2))
    const first = recordObservations.mock.calls[0][1].taken_at
    const second = recordObservations.mock.calls[1][1].taken_at
    expect(first).toBeTruthy()
    // Exact equality is what the server dedupes on. Anything else charts twice.
    expect(second).toBe(first)
  })
})

describe('drafts are scoped to the session, not to the device', () => {
  it('restores this nurse\'s own interrupted draft (the anti-data-loss feature)', async () => {
    writeDraft()
    renderSheet()
    expect(await screen.findByText(/were restored/i)).toBeInTheDocument()
    expect((screen.getByLabelText(/Respiratory rate/i) as HTMLInputElement).value).toBe('18')
  })

  it('never shows another nurse the previous nurse\'s draft, and deletes it', async () => {
    writeDraft({ ownerId: 'NUR999' })
    renderSheet()
    await waitFor(() =>
      expect((screen.getByLabelText(/Respiratory rate/i) as HTMLInputElement).value).toBe(''))
    expect(screen.queryByText(/were restored/i)).toBeNull()
    // Not merely hidden: gone from the device.
    expect(localStorage.getItem(obsDraftKey('P001'))).toBeNull()
  })

  it('refuses an unstamped legacy draft rather than guessing whose it is', async () => {
    writeDraft({ ownerId: undefined })
    renderSheet()
    await waitFor(() => expect(localStorage.getItem(obsDraftKey('P001'))).toBeNull())
    expect(screen.queryByText(/were restored/i)).toBeNull()
  })

  it('ages a draft out after a shift', async () => {
    writeDraft({ savedAt: Date.now() - NURSE_DRAFT_MAX_AGE_MS - 1000 })
    renderSheet()
    await waitFor(() => expect(localStorage.getItem(obsDraftKey('P001'))).toBeNull())
  })

  it('restores nothing when nobody is signed in', async () => {
    writeDraft()
    localStorage.removeItem('orb_user')
    renderSheet()
    await waitFor(() =>
      expect((screen.getByLabelText(/Respiratory rate/i) as HTMLInputElement).value).toBe(''))
    expect(localStorage.getItem(obsDraftKey('P001'))).toBeNull()
  })
})

describe('clearNurseDrafts', () => {
  it('removes every orb:nurse key and nothing else', () => {
    localStorage.setItem(obsDraftKey('P001'), '{}')
    localStorage.setItem('orb:nurse:handover-note:P002', 'shift note text')
    localStorage.setItem('orb_theme', 'dark')

    expect(clearNurseDrafts()).toBe(2)
    expect(localStorage.getItem(obsDraftKey('P001'))).toBeNull()
    expect(localStorage.getItem('orb:nurse:handover-note:P002')).toBeNull()
    expect(localStorage.getItem('orb_theme')).toBe('dark')
  })
})

/**
 * The per-field points chip, on a patient scored on SpO2 Scale 2.
 *
 * The server names the SpO2 breakdown row after the table that scored it:
 * "SpO2" on Scale 1, "SpO2 (Scale 2)" on Scale 2 (both labels are pinned by
 * server/tests/test_vigil_driver_labels.py). The sheet matched "SpO2" only, so
 * the one field whose points depend on the patient's scale was the one field
 * that never showed them, on exactly the COPD patients the Scale 2 note at the
 * top of the sheet is there for.
 */
describe('the SpO2 points chip follows the patient\'s scale', () => {
  const fieldRow = (label: RegExp) =>
    screen.getByLabelText(label).closest('.obs-field__row') as HTMLElement

  function previewWith(spo2Row: Record<string, unknown>, extra: Record<string, unknown> = {}) {
    previewNews2.mockResolvedValue({
      news2: {
        ...NEWS2,
        ...extra,
        breakdown: [
          spo2Row,
          { parameter: 'Supplemental O2', value: true, points: 2, note: 'On supplemental oxygen' },
          { parameter: 'Respiratory rate', value: 18, points: 0, note: null },
        ],
      },
      schedule: SCHEDULE,
    })
  }

  it('shows the Scale 2 SpO2 points beside the SpO2 field', async () => {
    previewWith(
      { parameter: 'SpO2 (Scale 2)', value: 97, points: 3, note: 'Over-oxygenation on O2 (97%, Scale 2)' },
      { raw_news2: 5, level: 'elevated', spo2_scale: 2 },
    )
    writeDraft({ onOxygen: true })
    const scale2 = { ...PATIENT, on_scale_2: true } as unknown as NursePatientCard
    render(<ObsEntrySheet patient={scale2} onClose={vi.fn()} onRecorded={vi.fn()} />)

    // The preview has landed: the RR field already carries its chip.
    await waitFor(() =>
      expect(within(fieldRow(/Respiratory rate/i)).getByTitle('Contributes 0 to NEWS2')).toBeInTheDocument())
    const chip = within(fieldRow(/Oxygen saturation/i)).getByTitle('Contributes 3 to NEWS2')
    expect(chip).toHaveTextContent('+3')
  })

  it('still shows the Scale 1 SpO2 points (control)', async () => {
    previewWith({ parameter: 'SpO2', value: 93, points: 2, note: 'Hypoxia (SpO2 93%)' }, { spo2_scale: 1 })
    writeDraft({ onOxygen: true })
    renderSheet()

    await waitFor(() =>
      expect(within(fieldRow(/Respiratory rate/i)).getByTitle('Contributes 0 to NEWS2')).toBeInTheDocument())
    expect(within(fieldRow(/Oxygen saturation/i)).getByTitle('Contributes 2 to NEWS2')).toHaveTextContent('+2')
  })
})
