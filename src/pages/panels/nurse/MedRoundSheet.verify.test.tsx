/**
 * medorders-11 and medorders-19 on the drug round (W8-I).
 *
 * The pharmacist check before the first dose (ruling MO-R1): a dose the
 * bedside will refuse until a pharmacist checks the order says so before the
 * walk to the bed and offers no Administer; a first stat or urgent dose the
 * bedside will let through says it will be marked; a dose given that way says
 * "Given before pharmacist check" on the round. Set times (ruling MO-R8) say
 * where they came from, and the footnote stops calling every time derived.
 * With neither feature on (no keys on the board), the round is unchanged.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { MedTask, NursePatientCard } from '../../../api/nurse'
import type { OfflineSnapshot } from '../../../hooks/useOfflineSnapshot'

const getAdministrations = vi.fn()

vi.mock('../../../api/client', () => ({
  getAdministrations: (...a: unknown[]) => getAdministrations(...a),
  givenAcrossAllergy: () => false,
  administerMedication: vi.fn(),
  getUser: () => null,
}))

/* The modal's own flow has its own suites; here it only hands back the record
   the server answered with. */
vi.mock('../../../components/vigil/AdministerModal', () => ({
  default: (props: { med: { id: number }; onRecorded: (r: unknown) => void }) => (
    <div role="dialog" aria-label="Administer stub">
      <button
        type="button"
        onClick={() => props.onRecorded({
          id: 90, medication_id: props.med.id, status: 'given', patient_id: 'P001',
          given_before_pharmacist_check: true,
        })}
      >
        Record stub dose
      </button>
    </div>
  ),
}))

import MedRoundSheet from './MedRoundSheet'

const LIVE: OfflineSnapshot<unknown> = {
  data: null, source: 'live', savedAt: Date.now(), online: true, unreachable: false,
}

const base: Omit<MedTask, 'medication_id' | 'drug_name'> = {
  dosage: '500 mg', route: 'PO', frequency: 'TDS', schedule_kind: 'scheduled',
  interval_minutes: 480, status: 'active', last_given_at: null, given_count: 0,
  due_at: '2026-10-10T08:30:00+00:00', due_basis: 'order_start', missed_slots: 0,
  minutes_until_due: 120, overdue: false, due_soon: false, unverified: false,
}

function patientWith(medications: MedTask[]): NursePatientCard {
  return {
    patient_id: 'P001', name: 'Asha Menon', mrn: 'MRN-1', bed: 'GM-4', ward: 'GM', age: 61,
    sex: 'F', diagnosis: null, department: 'General Medicine', attending_doctor_name: null,
    on_scale_2: false, allergies: [], news2: null, last_obs_at: null, obs_interval_minutes: 60,
    obs_band: 'Low', obs_response: '', obs_due_at: null, medications, meds_overdue: 0,
    meds_due_soon: 0,
  } as unknown as NursePatientCard
}

function open(medications: MedTask[], focusMedicationId?: number) {
  render(
    <MedRoundSheet
      patient={patientWith(medications)}
      focusMedicationId={focusMedicationId}
      offline={LIVE}
      onClose={() => {}}
      onAdministered={() => {}}
    />,
  )
}

const rowOf = (drug: string) => screen.getByText(drug).closest('li')!

beforeEach(() => {
  getAdministrations.mockReset()
  getAdministrations.mockResolvedValue([])
})

describe('MedRoundSheet: the pharmacist check before the first dose', () => {
  it('says a waiting order cannot be given and offers no live Administer', () => {
    open([{ ...base, medication_id: 1, drug_name: 'Amoxicillin',
      pharmacist_check: { state: 'waiting', message: 'x', priority: 'routine' } }])
    const row = rowOf('Amoxicillin')
    expect(within(row).getByText(/Waiting for a pharmacist to check this order/)).toBeTruthy()
    const button = within(row).getByRole('button', { name: 'Administer' }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
  })

  it('never opens straight into the modal for a waiting order', () => {
    open([{ ...base, medication_id: 1, drug_name: 'Amoxicillin',
      pharmacist_check: { state: 'waiting', message: 'x', priority: null } }], 1)
    expect(screen.queryByRole('dialog', { name: 'Administer stub' })).toBeNull()
  })

  it('offers a first stat dose and says it will be marked', () => {
    open([{ ...base, medication_id: 2, drug_name: 'Ceftriaxone',
      pharmacist_check: { state: 'first_dose_before_check', message: 'x', priority: 'stat' } }])
    const row = rowOf('Ceftriaxone')
    expect(within(row).getByText(/Stat order: the first dose can be given before the pharmacist/))
      .toBeTruthy()
    const button = within(row).getByRole('button', { name: 'Administer' }) as HTMLButtonElement
    expect(button.disabled).toBe(false)
  })

  it('names an urgent order as urgent', () => {
    open([{ ...base, medication_id: 2, drug_name: 'Ceftriaxone',
      pharmacist_check: { state: 'first_dose_before_check', message: 'x', priority: 'urgent' } }])
    expect(within(rowOf('Ceftriaxone')).getByText(/Urgent order: the first dose/)).toBeTruthy()
  })

  it('marks a dose given before the check the moment it is charted', () => {
    open([{ ...base, medication_id: 2, drug_name: 'Ceftriaxone',
      pharmacist_check: { state: 'first_dose_before_check', message: 'x', priority: 'stat' } }])
    fireEvent.click(within(rowOf('Ceftriaxone')).getByRole('button', { name: 'Administer' }))
    fireEvent.click(screen.getByRole('button', { name: 'Record stub dose' }))
    const row = rowOf('Ceftriaxone')
    expect(within(row).getByText('Given')).toBeTruthy()
    expect(within(row).getByText('Given before pharmacist check')).toBeTruthy()
    expect(within(row).queryByText(/first dose can be given/)).toBeNull()
  })

  it('marks a stat dose already charted before the check', async () => {
    getAdministrations.mockResolvedValue([{
      id: 5, medication_id: 3, patient_id: 'P001', drug_name: 'Adrenaline', status: 'given',
      administered_by_name: 'Sister Nair', administered_at: '2026-10-10 03:40:00',
      given_before_pharmacist_check: true,
    }])
    open([{ ...base, medication_id: 3, drug_name: 'Adrenaline', frequency: 'STAT',
      schedule_kind: 'stat', due_basis: 'stat_given', due_at: null, minutes_until_due: null,
      last_given_at: '2026-10-10T03:40:00+00:00', given_count: 1 }])
    await waitFor(() =>
      expect(within(rowOf('Adrenaline')).getByText('Given before pharmacist check')).toBeTruthy())
  })

  it('says nothing new while the feature is off (no key on the board)', () => {
    open([{ ...base, medication_id: 4, drug_name: 'Paracetamol' }])
    const row = rowOf('Paracetamol')
    expect(within(row).queryByText(/pharmacist/)).toBeNull()
    const button = within(row).getByRole('button', { name: 'Administer' }) as HTMLButtonElement
    expect(button.disabled).toBe(false)
  })
})

describe('MedRoundSheet: set times', () => {
  it("says a time was set by the prescriber, and the footnote stops calling it derived", () => {
    open([{ ...base, medication_id: 5, drug_name: 'Amoxicillin', due_source: 'prescriber',
      set_times: ['06:00', '14:00', '22:00'] }])
    expect(within(rowOf('Amoxicillin')).getByText(/time set by the prescriber/)).toBeTruthy()
    expect(screen.getByText(/Due times marked as set come from the prescriber/)).toBeTruthy()
    expect(screen.queryByText(/Orb holds no prescriber-authored administration schedule/)).toBeNull()
  })

  it("says a time is the hospital's set time, and a late one is not called derived", () => {
    open([{ ...base, medication_id: 6, drug_name: 'Metformin', due_source: 'site_times',
      set_times: ['08:00', '20:00'], overdue: true, minutes_until_due: -40 }])
    expect(within(rowOf('Metformin')).getByText(/hospital's set time/)).toBeTruthy()
    expect(screen.getByText('Past the due time by more than 15 minutes')).toBeTruthy()
  })

  it('keeps the old wording when nothing runs on set times', () => {
    open([{ ...base, medication_id: 7, drug_name: 'Paracetamol', overdue: true,
      minutes_until_due: -40 }])
    expect(screen.getByText(/Orb holds no prescriber-authored administration schedule/)).toBeTruthy()
    expect(screen.getByText('Past the derived due time by more than 15 minutes')).toBeTruthy()
  })
})
