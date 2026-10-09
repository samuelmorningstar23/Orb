/**
 * The ward round on an Orb Ward Record computer (Samuel's ruling, 9 October
 * 2026: hide the drug lists). The round names no medicine, counts no overdue
 * dose, asks for no pharmacist check and lists no medication story event, even
 * from a board or a story that still carries them. With the profile off every
 * one of these is as before.
 */
import { afterEach, describe, expect, it } from 'vitest'
import type { NursePatientCard } from '../../api/nurse'
import type { StoryEvent } from '../../api/client'
import { resetClinicalSwitches, setClinicalSwitches } from '../../shellConfig'
import { changesSince, currentIssues, decisionsToday } from './roundSummary'

const MED = {
  medication_id: 7, drug_name: 'Amoxicillin', dosage: '500 mg', route: 'Oral', frequency: 'TDS',
  schedule_kind: 'scheduled' as const, interval_minutes: 480, status: 'active',
  last_given_at: null, given_count: 0, due_at: null, due_basis: 'order_start' as const,
  missed_slots: 1, minutes_until_due: -30, overdue: true, due_soon: false, unverified: true,
}

function card(): NursePatientCard {
  return {
    patient_id: 'P1', name: 'A Patient', mrn: 'M1', bed: 'GM-1', ward: 'GM',
    age: 71, sex: 'F', diagnosis: 'CAP', department: 'General Medicine',
    attending_doctor_name: 'Dr Rao', on_scale_2: false, allergies: [],
    news2: null, last_obs_at: new Date().toISOString(), obs_interval_minutes: 240,
    obs_band: null, obs_response: null, obs_due_at: null, obs_minutes_until_due: 60,
    obs_due_source: 'ward_interval', obs_due_note: null,
    obs_overdue: false, obs_due_soon: false, medications: [MED],
    meds_overdue: 1, meds_due_soon: 0,
  } as NursePatientCard
}

const NOW = new Date()
const STORY: StoryEvent[] = [
  { timestamp: NOW.toISOString(), type: 'medication', title: 'Started Amoxicillin', detail: '500 mg', actor: null },
  { timestamp: NOW.toISOString(), type: 'administration', title: 'Gave Amoxicillin', detail: '500 mg', actor: null },
  { timestamp: NOW.toISOString(), type: 'note', title: 'PROGRESS note', detail: 'Settled', actor: null },
]
const WINDOW = { start: new Date(NOW.getTime() - 3_600_000), sinceLabel: 'an hour ago' }
const MEDICINE = /medicine|medication|Amoxicillin|500 mg/i

afterEach(() => { resetClinicalSwitches() })

function everything(): string {
  return JSON.stringify([
    currentIssues(card(), []), decisionsToday(card(), [], true),
    changesSince(STORY, WINDOW), currentIssues(null, []),
  ])
}

describe('the round and the drug chart', () => {
  it('names no medicine on a Ward Record computer', () => {
    setClinicalSwitches({ ward_record: true, scoring_enabled: false, models_enabled: false })
    expect(everything()).not.toMatch(MEDICINE)
    expect(JSON.stringify(changesSince(STORY, WINDOW))).toContain('PROGRESS note')
  })

  it('names the overdue dose, the unverified drug and the story lines with the profile off', () => {
    expect(currentIssues(card(), []).map(i => i.id)).toContain('meds-overdue')
    expect(decisionsToday(card(), [], true).map(d => d.id)).toEqual(['med-7'])
    expect(JSON.stringify(changesSince(STORY, WINDOW))).toContain('Gave Amoxicillin')
  })
})
