/**
 * The round's empty states on an Orb Ward Record computer (Samuel's ruling, 9
 * October 2026: hide the drug lists): "No active issues" and "Nothing
 * outstanding" say nothing about medicines, not even that none is overdue.
 */
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import RoundSummaryView from './RoundSummaryView'
import type { RoundPatient } from './roundQueue'
import type { NursePatientCard } from '../../api/nurse'
import { resetClinicalSwitches, setClinicalSwitches } from '../../shellConfig'

const PATIENT: RoundPatient = {
  id: 'P1', name: 'Anita Rao', bed: 'GM-102', ward: 'GM', mrn: 'MRN-P1',
  age: 62, sex: 'F', diagnosis: 'Pneumonia', admission_date: '2026-10-05',
  risk_score: null, risk_level: null, risk_drivers: [], attending_doctor_name: 'Dr Iyer',
} as unknown as RoundPatient

const NOW = new Date(2026, 9, 9, 9, 0, 0)

const CARD = {
  patient_id: 'P1', name: 'Anita Rao', mrn: 'MRN-P1', bed: 'GM-102', ward: 'GM',
  age: 62, sex: 'F', diagnosis: 'Pneumonia', department: 'Surgery',
  attending_doctor_name: 'Dr Iyer', on_scale_2: false, allergies: [],
  news2: null, last_obs_at: new Date(NOW.getTime() - 3_600_000).toISOString(),
  obs_interval_minutes: 240, obs_band: null, obs_response: null, obs_due_at: null,
  obs_minutes_until_due: 60, obs_due_source: 'ward_interval', obs_due_note: null,
  obs_overdue: false, obs_due_soon: false, medications: [], meds_overdue: null, meds_due_soon: null,
} as unknown as NursePatientCard

function renderSummary() {
  return render(
    <RoundSummaryView patient={PATIENT} card={CARD} storyPhase="ready" story={[]}
      storyError={null} onRetryStory={() => {}} insights={[]} insightsFailed={false}
      hasPlanToday assessment={null} now={NOW} />,
  )
}

afterEach(() => { cleanup(); resetClinicalSwitches() })

describe('the round empty states and the drug chart', () => {
  it('say nothing about medicines on a Ward Record computer', () => {
    setClinicalSwitches({ ward_record: true, scoring_enabled: false, models_enabled: false })
    const { container } = renderSummary()
    expect(container.textContent).toContain('No active issues')
    expect(container.textContent).toContain('Nothing outstanding')
    expect(container.textContent).not.toMatch(/medicine|medication/i)
  })

  it('say no medication is overdue with the profile off', () => {
    const { container } = renderSummary()
    expect(container.textContent).toMatch(/no medication is overdue/)
    expect(container.textContent).toMatch(/No unverified medications/)
  })
})
