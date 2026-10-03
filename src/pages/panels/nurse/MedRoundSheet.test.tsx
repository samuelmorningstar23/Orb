/**
 * A STAT DOSE IS GIVEN ONCE.
 *
 * The board marks a charted stat order `due_basis: 'stat_given'` and gives it no
 * due time. The round sheet used to drop it into "Later this shift" with a live
 * Administer button, and the server only WARNS on the repeat, so a second nurse
 * could chart the same stat dose again. It now sits under "Already given", with
 * when and by whom, and nothing to press.
 */
import { render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { MedTask, NursePatientCard } from '../../../api/nurse'
import type { OfflineSnapshot } from '../../../hooks/useOfflineSnapshot'

const getAdministrations = vi.fn()

vi.mock('../../../api/client', () => ({
  getAdministrations: (...a: unknown[]) => getAdministrations(...a),
  givenAcrossAllergy: (r: { status?: string; allergy_check?: { override?: { reason?: string } } } | null) =>
    Boolean(r?.status === 'given' && r?.allergy_check?.override?.reason),
  administerMedication: vi.fn(),
  getUser: () => null,
}))

import MedRoundSheet from './MedRoundSheet'

const LIVE: OfflineSnapshot<unknown> = {
  data: null, source: 'live', savedAt: Date.now(), online: true, unreachable: false,
}

const base: Omit<MedTask, 'medication_id' | 'drug_name' | 'schedule_kind' | 'due_basis'> = {
  dosage: '1 g', route: 'IV', frequency: 'STAT', interval_minutes: null, status: 'active',
  last_given_at: null, given_count: 0, due_at: null, missed_slots: 0,
  minutes_until_due: null, overdue: false, due_soon: false, unverified: false,
}

const STAT_GIVEN: MedTask = {
  ...base, medication_id: 11, drug_name: 'Ceftriaxone', schedule_kind: 'stat',
  due_basis: 'stat_given', last_given_at: '2026-10-02T03:40:00+00:00', given_count: 1,
}
const SCHEDULED_LATER: MedTask = {
  ...base, medication_id: 12, drug_name: 'Paracetamol', dosage: '1 g', route: 'PO',
  frequency: 'QDS', schedule_kind: 'scheduled', interval_minutes: 360, due_basis: 'last_given',
  due_at: '2026-10-02T12:00:00+00:00', minutes_until_due: 180,
}

const PATIENT = {
  patient_id: 'P001', name: 'Asha Menon', mrn: 'MRN-1', bed: 'GM-4', ward: 'GM', age: 61, sex: 'F',
  diagnosis: null, department: 'General Medicine', attending_doctor_name: null, on_scale_2: false,
  allergies: [], news2: null, last_obs_at: null, obs_interval_minutes: 60, obs_band: 'Low',
  obs_response: '', obs_due_at: null, medications: [STAT_GIVEN, SCHEDULED_LATER],
  meds_overdue: 0, meds_due_soon: 0,
} as unknown as NursePatientCard

function open() {
  render(
    <MedRoundSheet patient={PATIENT} offline={LIVE} onClose={() => {}} onAdministered={() => {}} />,
  )
}

/** The <section> a heading belongs to. */
const groupOf = (title: RegExp) => screen.getByRole('heading', { name: title }).closest('section')!

describe('MedRoundSheet: a stat dose already charted', () => {
  beforeEach(() => {
    getAdministrations.mockReset()
    getAdministrations.mockResolvedValue([
      {
        id: 5, medication_id: 11, patient_id: 'P001', drug_name: 'Ceftriaxone', status: 'given',
        dose_given: '1 g', route_given: 'IV', not_given_reason: null, administered_by: 'NUR002',
        administered_by_name: 'Sister Nair', administered_at: '2026-10-02 03:40:00',
      },
    ])
  })

  it('sits under "Already given" with no Administer button', async () => {
    open()
    const given = groupOf(/Already given/)
    expect(within(given).getByText('Ceftriaxone')).toBeTruthy()
    expect(within(given).queryByRole('button', { name: /Administer/ })).toBeNull()
    expect(within(given).getByText('Given')).toBeTruthy()

    // Not offered again under any other heading either.
    expect(screen.queryByRole('heading', { name: /Later this shift/ })).toBeTruthy()
    const later = groupOf(/Later this shift/)
    expect(within(later).queryByText('Ceftriaxone')).toBeNull()
    expect(within(later).getByRole('button', { name: /Administer/ })).toBeTruthy()
    // Let the who-gave-it lookup settle inside the test.
    await waitFor(() => expect(given.textContent).toMatch(/Sister Nair/))
  })

  it('says when it was given and who gave it', async () => {
    open()
    const given = groupOf(/Already given/)
    expect(given.textContent).toMatch(/Given 2 Oct 2026/)
    await waitFor(() => expect(given.textContent).toMatch(/by Sister Nair/))
    expect(getAdministrations).toHaveBeenCalledWith('P001')
  })

  it('still shows the time when the history cannot be read', async () => {
    getAdministrations.mockRejectedValue(new Error('Failed to fetch'))
    open()
    const given = groupOf(/Already given/)
    expect(given.textContent).toMatch(/Given 2 Oct 2026/)
    expect(within(given).queryByRole('button', { name: /Administer/ })).toBeNull()
  })

  it('does not open the administer screen when focused on a stat dose already given', async () => {
    render(
      <MedRoundSheet
        patient={PATIENT}
        focusMedicationId={STAT_GIVEN.medication_id}
        offline={LIVE}
        onClose={() => {}}
        onAdministered={() => {}}
      />,
    )
    expect(screen.queryByRole('heading', { name: /Administer medication/ })).toBeNull()
    expect(within(groupOf(/Already given/)).getByText('Ceftriaxone')).toBeTruthy()
    await waitFor(() => expect(getAdministrations).toHaveBeenCalled())
  })

  it('does not fetch history when no stat dose has been given', () => {
    render(
      <MedRoundSheet
        patient={{ ...PATIENT, medications: [SCHEDULED_LATER] }}
        offline={LIVE}
        onClose={() => {}}
        onAdministered={() => {}}
      />,
    )
    expect(getAdministrations).not.toHaveBeenCalled()
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   THE ALLERGY BLOCK THE BOARD ALREADY KNOWS ABOUT (F054)
   nurse_shift_service runs the bedside allergy interlock on every dose and
   sends its verdict on `allergy`. The round used to drop it, so the
   Administer screen opened in amber ("no charted allergen is named...") for a
   cephalosporin the server would refuse on a penicillin anaphylaxis.
   ══════════════════════════════════════════════════════════════════════════ */
const BLOCKED: MedTask = {
  ...base, medication_id: 21, drug_name: 'Cefuroxime', dosage: '750 mg', route: 'IV',
  frequency: 'TDS', schedule_kind: 'scheduled', interval_minutes: 480, due_basis: 'last_given',
  due_at: '2026-10-02T12:00:00+00:00', minutes_until_due: 10, due_soon: true,
  allergy: {
    decision: 'block', screen_failed: false, override: null,
    override_requires_permission: 'medication.prescribe',
    alerts: [{
      substance: 'Penicillin', match: 'cross_reactivity', decision: 'block',
      effective_severity: 'severe',
      message: 'Cefuroxime is a cephalosporin; patient has a severe penicillin allergy (anaphylaxis).',
    }],
  },
}

describe('MedRoundSheet: a dose the allergy screen will refuse', () => {
  const blockedPatient = {
    ...PATIENT,
    allergies: [{ substance: 'Penicillin', reaction: 'anaphylaxis', severity: 'severe' }],
    medications: [BLOCKED],
  } as unknown as NursePatientCard

  it('says so on the row, before the walk to the bed', () => {
    render(<MedRoundSheet patient={blockedPatient} offline={LIVE} onClose={() => {}} onAdministered={() => {}} />)
    expect(screen.getByText('Allergy block: needs prescriber authorisation')).toBeTruthy()
    // Still openable: the nurse may need to record it as not given.
    expect(screen.getByRole('button', { name: /Administer/ })).not.toBeDisabled()
  })

  it('opens the administer screen in the danger state with the server\'s finding', () => {
    render(
      <MedRoundSheet
        patient={blockedPatient} focusMedicationId={21} offline={LIVE}
        onClose={() => {}} onAdministered={() => {}}
      />,
    )
    const heading = screen.getByRole('heading', { name: /Allergy block: needs prescriber authorisation/ })
    const panel = heading.closest('section')!
    expect(panel.className).toMatch(/--danger/)
    expect(within(panel).getByText(/cephalosporin; patient has a severe penicillin allergy/)).toBeTruthy()
    expect(within(panel).getByText(/Contact the prescriber, or record it as Not given/)).toBeTruthy()
    // Not the amber "no charted allergen is named" reassurance.
    expect(screen.queryByText(/No charted allergen is named/)).toBeNull()
  })

  it('shows no block line once a prescriber has authorised this order', () => {
    const authorised = {
      ...blockedPatient,
      medications: [{ ...BLOCKED, allergy: { ...BLOCKED.allergy!, override: { reason: 'Desensitised', carried: true } } }],
    } as unknown as NursePatientCard
    render(<MedRoundSheet patient={authorised} offline={LIVE} onClose={() => {}} onAdministered={() => {}} />)
    expect(screen.queryByText('Allergy block: needs prescriber authorisation')).toBeNull()
  })

  it('an empty allergy list reads as none recorded, never NKDA', () => {
    render(
      <MedRoundSheet
        patient={{ ...PATIENT, medications: [SCHEDULED_LATER] }}
        offline={LIVE} onClose={() => {}} onAdministered={() => {}}
      />,
    )
    expect(screen.getByText('No allergies recorded. Confirm with the patient.')).toBeTruthy()
    expect(screen.queryByText(/NKDA/)).toBeNull()
  })
})
