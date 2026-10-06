/**
 * The ward round's current issues with scoring switched off (the Orb Ward
 * Record profile, 6 October 2026). The board then sends news2 null and
 * obs_band null for every patient. The round must draw no NEWS2 line and must
 * not call a patient with sets on file "no scored observation set".
 */
import { describe, expect, it } from 'vitest'
import type { NursePatientCard } from '../../api/nurse'
import { currentIssues } from './roundSummary'

function card(over: Partial<NursePatientCard> = {}): NursePatientCard {
  return {
    patient_id: 'P1', name: 'A Patient', mrn: 'M1', bed: 'GM-1', ward: 'GM',
    age: 71, sex: 'F', diagnosis: 'CAP', department: 'General Medicine',
    attending_doctor_name: 'Dr Rao', on_scale_2: false, allergies: [],
    news2: null, last_obs_at: '2026-10-06T06:00:00Z', obs_interval_minutes: 240,
    obs_band: null, obs_response: null, obs_due_at: null, obs_minutes_until_due: 60,
    obs_due_source: 'ward_interval', obs_due_note: null,
    obs_overdue: false, obs_due_soon: false, medications: [],
    meds_overdue: 0, meds_due_soon: 0,
    ...over,
  }
}

describe('the round with scoring switched off', () => {
  it('draws no NEWS2 line and no missing score for a patient with sets on file', () => {
    const issues = currentIssues(card(), [])
    expect(issues.map(i => i.id)).toEqual([])
    expect(issues.some(i => /NEWS2|scored/.test(`${i.label} ${i.detail}`))).toBe(false)
  })

  it('still says when no set has been recorded at all, without mentioning a score', () => {
    const issues = currentIssues(card({ last_obs_at: null }), [])
    expect(issues).toHaveLength(1)
    expect(issues[0].label).toBe('No observations recorded')
    expect(issues[0].detail).not.toMatch(/score/)
  })

  it('a late round on the ward interval is still an overdue line', () => {
    const issues = currentIssues(card({ obs_overdue: true }), [])
    expect(issues.map(i => i.id)).toEqual(['obs-overdue'])
    expect(issues[0].detail).toContain('due every 240 min')
  })
})
