/**
 * WHAT THE ROUND SHEET SAYS ABOUT A DOSE AFTER IT IS CHARTED (F133).
 *
 *   · A dose charted NOT GIVEN took the same green left rule as a dose given.
 *     Not given is a recorded decision, not a completed dose; green on this
 *     sheet means the drug went in.
 *   · A continuous infusion's group said "do not chart" above a row offering
 *     Administer, and the row said "Continuous" again under the heading that
 *     already said it.
 *   · "168 earlier doses not charted" is noise; past a dozen it reads "12+".
 *
 * The administer modal is stubbed: its own guards are tested beside it, and
 * what is under test here is only what the sheet does with the record it hands
 * back.
 */
import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { MedTask, NursePatientCard } from '../../../api/nurse'
import type { OfflineSnapshot } from '../../../hooks/useOfflineSnapshot'

vi.mock('../../../api/client', () => ({
  getAdministrations: vi.fn().mockResolvedValue([]),
  givenAcrossAllergy: () => false,
}))

vi.mock('../../../components/vigil/AdministerModal', () => ({
  default: ({ med, onRecorded }: { med: { id: number; drug_name: string }; onRecorded: (r: unknown) => void }) => (
    <div>
      <button
        type="button"
        onClick={() => onRecorded({ id: 1, medication_id: med.id, drug_name: med.drug_name, status: 'not_given' })}
      >
        Stub: record not given
      </button>
      <button
        type="button"
        onClick={() => onRecorded({ id: 2, medication_id: med.id, drug_name: med.drug_name, status: 'given' })}
      >
        Stub: record given
      </button>
    </div>
  ),
}))

import MedRoundSheet from './MedRoundSheet'

const LIVE: OfflineSnapshot<unknown> = {
  data: null, source: 'live', savedAt: Date.now(), online: true, unreachable: false,
}

const LATE_DOSE: MedTask = {
  medication_id: 31, drug_name: 'Paracetamol', dosage: '1 g', route: 'PO', frequency: 'QDS',
  schedule_kind: 'scheduled', interval_minutes: 360, status: 'active', last_given_at: null,
  given_count: 0, due_at: '2026-10-02T06:00:00+00:00', due_basis: 'order_start', missed_slots: 0,
  minutes_until_due: -40, overdue: true, due_soon: false, unverified: false,
}

const INFUSION: MedTask = {
  ...LATE_DOSE, medication_id: 32, drug_name: 'Heparin', dosage: '1000 units/h', route: 'IV',
  frequency: 'continuous', schedule_kind: 'continuous', interval_minutes: null, due_at: null,
  due_basis: 'unscheduled', minutes_until_due: null, overdue: false,
}

function patientWith(medications: MedTask[]): NursePatientCard {
  return {
    patient_id: 'P001', name: 'Asha Menon', mrn: 'MRN-1', bed: 'GM-4', ward: 'GM', age: 61, sex: 'F',
    diagnosis: null, department: 'General Medicine', attending_doctor_name: null, on_scale_2: false,
    allergies: [], news2: null, last_obs_at: null, obs_interval_minutes: 60, obs_band: 'Low',
    obs_response: '', obs_due_at: null, obs_minutes_until_due: null, obs_overdue: false,
    obs_due_soon: false, medications, meds_overdue: 0, meds_due_soon: 0,
  } as NursePatientCard
}

const rowOf = (drug: string) => screen.getByText(drug).closest('li')!

function chart(drug: string, outcome: 'given' | 'not given') {
  fireEvent.click(within(rowOf(drug)).getByRole('button', { name: 'Administer' }))
  fireEvent.click(screen.getByRole('button', { name: `Stub: record ${outcome}` }))
}

describe('a dose charted on the round', () => {
  it('takes the held rule, never the green one, when charted not given', () => {
    render(<MedRoundSheet patient={patientWith([LATE_DOSE])} offline={LIVE} onClose={() => {}} onAdministered={() => {}} />)
    chart('Paracetamol', 'not given')

    const row = rowOf('Paracetamol')
    expect(row.className).toContain('med-row--held')
    expect(row.className).not.toContain('med-row--done')
    expect(within(row).getByText('Not given').className).toContain('med-row__result--not_given')
  })

  it('takes the green rule when charted given', () => {
    render(<MedRoundSheet patient={patientWith([LATE_DOSE])} offline={LIVE} onClose={() => {}} onAdministered={() => {}} />)
    chart('Paracetamol', 'given')

    const row = rowOf('Paracetamol')
    expect(row.className).toContain('med-row--done')
    expect(row.className).not.toContain('med-row--held')
  })
})

describe('a continuous infusion', () => {
  it('says it has no due time, once, and never tells the nurse not to chart it', () => {
    render(<MedRoundSheet patient={patientWith([INFUSION])} offline={LIVE} onClose={() => {}} onAdministered={() => {}} />)

    const group = screen.getByRole('heading', { name: /^Continuous/ }).closest('section')!
    expect(group.textContent).not.toMatch(/do not chart/i)
    expect(group.querySelector('.med-group__hint')!.textContent).toMatch(/No due time/)

    const timing = rowOf('Heparin').querySelector('.med-row__timing')!
    expect(timing.textContent).toBe('No due time')
  })
})

describe('earlier doses not charted', () => {
  it('caps the count at 12+', () => {
    render(
      <MedRoundSheet
        patient={patientWith([{ ...LATE_DOSE, missed_slots: 168 }])}
        offline={LIVE} onClose={() => {}} onAdministered={() => {}}
      />,
    )
    const missed = rowOf('Paracetamol').querySelector('.med-row__missed')!
    expect(missed.textContent).toBe('12+ earlier doses not charted')
  })

  it('keeps an exact count up to the cap', () => {
    render(
      <MedRoundSheet
        patient={patientWith([{ ...LATE_DOSE, missed_slots: 1 }])}
        offline={LIVE} onClose={() => {}} onAdministered={() => {}}
      />,
    )
    expect(rowOf('Paracetamol').querySelector('.med-row__missed')!.textContent).toBe('1 earlier dose not charted')
  })
})
