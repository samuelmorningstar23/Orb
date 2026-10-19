/**
 * The drug round's 'Awaiting pharmacist' group (medorders-10, W9-D).
 *
 * With the server's order store on, every dose the bedside will refuse until a
 * pharmacist checks the order (an AI-charted drug, or the check before the
 * first dose) is grouped under 'Awaiting pharmacist', still with no live
 * Administer. A first stat dose the bedside lets through stays where its due
 * time puts it. With the store off the round is grouped exactly as before.
 */
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MedTask, NursePatientCard } from '../../../api/nurse'
import type { OfflineSnapshot } from '../../../hooks/useOfflineSnapshot'

vi.mock('../../../api/client', () => ({
  getAdministrations: vi.fn(async () => []),
  givenAcrossAllergy: () => false,
  administerMedication: vi.fn(),
  getUser: () => null,
}))

import MedRoundSheet from './MedRoundSheet'
import { resetFeatureFlags, setFeatureFlags } from '../../../shellConfig'

const LIVE: OfflineSnapshot<unknown> = {
  data: null, source: 'live', savedAt: Date.now(), online: true, unreachable: false,
}

const base: Omit<MedTask, 'medication_id' | 'drug_name'> = {
  dosage: '500 mg', route: 'PO', frequency: 'TDS', schedule_kind: 'scheduled',
  interval_minutes: 480, status: 'active', last_given_at: null, given_count: 0,
  due_at: '2026-10-10T08:30:00+00:00', due_basis: 'order_start', missed_slots: 0,
  minutes_until_due: 10, overdue: false, due_soon: true, unverified: false,
}

const MEDS: MedTask[] = [
  { ...base, medication_id: 1, drug_name: 'Amoxicillin',
    pharmacist_check: { state: 'waiting', message: 'x', priority: 'routine' } },
  { ...base, medication_id: 2, drug_name: 'Metformin', unverified: true },
  { ...base, medication_id: 3, drug_name: 'Ceftriaxone',
    pharmacist_check: { state: 'first_dose_before_check', message: 'x', priority: 'stat' } },
  { ...base, medication_id: 4, drug_name: 'Paracetamol' },
]

function open() {
  render(
    <MedRoundSheet
      patient={{
        patient_id: 'P001', name: 'Asha Menon', mrn: 'MRN-1', bed: 'GM-4', ward: 'GM', age: 61,
        sex: 'F', diagnosis: null, department: 'General Medicine', attending_doctor_name: null,
        on_scale_2: false, allergies: [], news2: null, last_obs_at: null, obs_interval_minutes: 60,
        medications: MEDS,
      } as unknown as NursePatientCard}
      offline={LIVE}
      onClose={() => {}}
      onAdministered={() => {}}
    />,
  )
}

const groupOf = (title: string) =>
  screen.getByRole('heading', { name: new RegExp(`^${title}`) }).closest('section') as HTMLElement

beforeEach(() => { resetFeatureFlags() })
afterEach(() => { cleanup(); resetFeatureFlags() })

describe('MedRoundSheet: Awaiting pharmacist (medorders-10)', () => {
  it('groups every dose waiting for a pharmacist while the order store is on', () => {
    setFeatureFlags({ med_order_store: true })
    open()
    const group = groupOf('Awaiting pharmacist')
    expect(within(group).getByText('Amoxicillin')).toBeTruthy()
    expect(within(group).getByText('Metformin')).toBeTruthy()
    expect(within(group).queryByText('Ceftriaxone')).toBeNull()
    expect(within(group).queryByText('Paracetamol')).toBeNull()
    for (const button of within(group).getAllByRole('button', { name: 'Administer' })) {
      expect((button as HTMLButtonElement).disabled).toBe(true)
    }
    const due = groupOf('Due now')
    expect(within(due).getByText('Ceftriaxone')).toBeTruthy()
    expect(within(due).getByText('Paracetamol')).toBeTruthy()
  })

  it('draws no such group with the order store off', () => {
    open()
    expect(screen.queryByRole('heading', { name: /^Awaiting pharmacist/ })).toBeNull()
    expect(within(groupOf('Due now')).getByText('Amoxicillin')).toBeTruthy()
    expect(within(groupOf('Due now')).getByText('Metformin')).toBeTruthy()
  })
})
