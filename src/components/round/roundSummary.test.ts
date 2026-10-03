/**
 * WARD ROUND — the one-screen summary, and what it is allowed to say.
 *
 * The doctor gets roughly 90 seconds at the bed and this screen does not
 * scroll, so everything it leaves out is a decision. These tests pin the
 * decisions that are clinical rather than cosmetic:
 *
 *   · ABSENCE IS NEVER "FINE". A patient with no observations recorded must
 *     produce a visible problem, not an empty issues list that reads as stable.
 *     This is the same failure the product already fixed once in the Sage
 *     sidebar (see components/errorStates.test.tsx) and it is far worse here,
 *     because this screen is the doctor's whole view of the patient.
 *   · NOTHING IS SILENTLY DROPPED. A no-scroll screen must cap the overnight
 *     feed, so it has to say how many events it is not showing.
 *   · THE SINGLE-PARAMETER-3 RULE SURVIVES. A NEWS2 aggregate of 3 with one
 *     parameter scoring 3 is the case most likely to be missed by someone
 *     scanning for big numbers, so it cannot be flattened into "low".
 *   · NO INVENTED SCORING. Everything clinical on this screen comes from a
 *     server-computed field. The panel classifies and orders; it never scores.
 */
import { describe, expect, it } from 'vitest'
import type { NursePatientCard } from '../../api/nurse'
import type { SageInsight, StoryEvent } from '../../api/client'
import type { DatedInsight } from '../sage/insightTime'
import {
  admissionDay,
  changesSince,
  currentIssues,
  decisionsToday,
  documentedToday,
  overnightWindow,
} from './roundSummary'

const NOW = new Date(2026, 7, 7, 9, 30) // 07 Aug 2026, 09:30 local

function card(over: Partial<NursePatientCard> = {}): NursePatientCard {
  return {
    patient_id: 'P1', name: 'A Patient', mrn: 'M1', bed: 'GM-1', ward: 'GM',
    age: 71, sex: 'F', diagnosis: 'CAP', department: 'General Medicine',
    attending_doctor_name: 'Dr Rao', on_scale_2: false, allergies: [],
    news2: null, last_obs_at: null, obs_interval_minutes: 720, obs_band: 'low',
    obs_response: 'routine', obs_due_at: null, obs_minutes_until_due: null,
    obs_overdue: false, obs_due_soon: false, medications: [],
    meds_overdue: 0, meds_due_soon: 0,
    ...over,
  }
}

function news2(over: Partial<NonNullable<NursePatientCard['news2']>> = {}) {
  return {
    score: 0.2, level: 'stable' as const, raw_news2: 1, single_param_3: false,
    drivers: [], breakdown: [], scoring_model: 'news2', spo2_scale: 1 as const,
    ...over,
  }
}

function ev(ts: string, type: StoryEvent['type'], title: string, detail = ''): StoryEvent {
  return { timestamp: ts, type, title, detail, actor: null }
}

/** A tz-suffixed timestamp for a given LOCAL wall-clock time.
 *
 *  parseUTC passes tz-suffixed strings through untouched, so these assertions
 *  mean the same thing in IST (the pilot ward), UTC (CI) and anywhere else.
 *  Bare "YYYY-MM-DD HH:MM:SS" strings — the other dialect the API returns — are
 *  exercised deliberately in their own test below. */
function at(y: number, m: number, d: number, h: number, mi = 0): string {
  return new Date(y, m, d, h, mi).toISOString()
}

describe('the overnight window is a stated time range, not a vague "recent"', () => {
  it('opens at the previous evening handover for a morning round', () => {
    const w = overnightWindow(NOW)
    expect(w.start).toEqual(new Date(2026, 7, 6, 20, 0, 0, 0))
    expect(w.sinceLabel).toBe('08:00 PM yesterday')
  })

  it('opens at this evening once the evening handover has passed', () => {
    const w = overnightWindow(new Date(2026, 7, 7, 22, 15))
    expect(w.start).toEqual(new Date(2026, 7, 7, 20, 0, 0, 0))
    expect(w.sinceLabel).toBe('08:00 PM today')
  })
})

describe('what changed overnight', () => {
  const w = overnightWindow(NOW)

  it('keeps in-window events newest first and drops older ones', () => {
    const feed = changesSince([
      ev(at(2026, 7, 7, 2, 0), 'alert', 'Critical alert'),
      ev(at(2026, 7, 5, 11, 0), 'note', 'Old note'),
      ev(at(2026, 7, 7, 6, 30), 'administration', 'Administered'),
    ], w)
    expect(feed.events.map(e => e.title)).toEqual(['Administered', 'Critical alert'])
    expect(feed.hidden).toBe(0)
  })

  it('caps the feed for a screen that does not scroll and says how many it hid', () => {
    const many = Array.from({ length: 9 }, (_, i) =>
      ev(at(2026, 7, 7, i, 0), 'administration', `Dose ${i}`))
    const feed = changesSince(many, w, 5)
    expect(feed.events).toHaveLength(5)
    expect(feed.hidden).toBe(4)
    // Newest kept, oldest hidden — never the other way round.
    expect(feed.events[0].title).toBe('Dose 8')
  })

  it('ignores events with no timestamp rather than dating them to now', () => {
    const feed = changesSince([
      { timestamp: null, type: 'note', title: 'Undated', detail: '', actor: null },
    ], w)
    expect(feed.events).toHaveLength(0)
  })

  it('reads bare SQLite timestamps as UTC, not as local time', () => {
    // "2026-08-07 01:00:00" is 01:00 UTC = 06:30 IST — inside the window either
    // way. The trap is an event just before the boundary: 2026-08-06 15:00 UTC
    // is 20:30 IST, i.e. AFTER the 20:00 handover, and must be included.
    const feed = changesSince([ev('2026-08-06 15:00:00', 'note', 'Evening review')], w)
    const included = feed.events.some(e => e.title === 'Evening review')
    // Assert against the same parse the app uses, so this is timezone-portable.
    const expected = new Date(Date.parse('2026-08-06T15:00:00Z')) >= w.start
    expect(included).toBe(expected)
  })
})

describe('current issues — absence of data is itself an issue', () => {
  it('says so loudly when no observations have ever been recorded', () => {
    const issues = currentIssues(card({ news2: null }), [])
    const missing = issues.find(i => i.source === 'observation')
    expect(missing).toBeDefined()
    expect(missing!.severity).toBe('warning')
    expect(missing!.label).toMatch(/no observations/i)
  })

  it('never returns an empty list for a patient with no data', () => {
    // An empty "Current issues" panel reads as "nothing wrong". For a patient
    // Orb knows nothing about, that is a false all-clear.
    expect(currentIssues(card(), [])).not.toHaveLength(0)
  })

  it('is empty only when the server actually scored the patient as stable', () => {
    const stable = card({ news2: news2({ raw_news2: 0, level: 'stable' }), last_obs_at: '2026-08-07 07:00:00' })
    expect(currentIssues(stable, [])).toHaveLength(0)
  })

  it('carries the server NEWS2 through without rescoring it', () => {
    const c = card({
      news2: news2({ raw_news2: 7, level: 'critical', drivers: ['RR 26', 'SpO2 91'] }),
      last_obs_at: '2026-08-07 07:00:00',
    })
    const issue = currentIssues(c, []).find(i => i.source === 'news2')!
    expect(issue.severity).toBe('critical')
    expect(issue.label).toContain('7')
    expect(issue.detail).toContain('RR 26')
  })

  it('does not let a single parameter scoring 3 hide behind a low aggregate', () => {
    const c = card({
      news2: news2({ raw_news2: 3, level: 'stable', single_param_3: true }),
      last_obs_at: '2026-08-07 07:00:00',
    })
    const issue = currentIssues(c, []).find(i => i.source === 'news2')!
    expect(issue.severity).toBe('warning')
    expect(issue.detail).toMatch(/single parameter/i)
  })

  it('surfaces overdue observations and overdue medications', () => {
    const c = card({
      news2: news2({ raw_news2: 0, level: 'stable' }), last_obs_at: '2026-08-07 01:00:00',
      obs_overdue: true, meds_overdue: 2,
    })
    const sources = currentIssues(c, []).map(i => i.source)
    expect(sources).toContain('observation')
    expect(sources).toContain('medication')
  })

  it('folds in Sage insights for this patient, critical first', () => {
    const insights: SageInsight[] = [
      { id: 'i1', patient_id: 'P1', insight_type: 'trend', severity: 'info', title: 'Slow response', state: 'new', created_at: '2026-08-07 05:00:00' },
      // Refreshed by the sweep after the latest set, so it still stands.
      { id: 'i2', patient_id: 'P1', insight_type: 'sepsis', severity: 'critical', title: 'Sepsis criteria met', state: 'new', created_at: '2026-08-07 06:00:00', updated_at: '2026-08-07 07:00:15' } as DatedInsight,
    ]
    const c = card({ news2: news2({ raw_news2: 0, level: 'stable' }), last_obs_at: '2026-08-07 07:00:00' })
    const issues = currentIssues(c, insights)
    expect(issues[0].severity).toBe('critical')
    expect(issues[0].detail || issues[0].label).toContain('Sepsis')
  })

  it('dates each Sage detail with the time its numbers were read (F031)', () => {
    const insights: DatedInsight[] = [{
      id: 'i1', patient_id: 'P1', insight_type: 'trajectory', severity: 'warning',
      title: 'SpO2 falling', detail: 'SpO2 trending down 96% to 91% over the last 3 readings.',
      state: 'active', created_at: '2026-08-07 06:00:00', updated_at: '2026-08-07 07:05:00',
    }]
    const c = card({ news2: news2({ raw_news2: 0, level: 'stable' }), last_obs_at: '2026-08-07 07:00:00' })
    const [issue] = currentIssues(c, insights)
    expect(issue.detail).toMatch(/^At .+: SpO2 trending down 96% to 91%/)
  })

  it('drops a vitals insight the latest observation has overtaken (F031)', () => {
    const insights: DatedInsight[] = [
      // Raised at 06:00 and not refreshed since: the 07:00 set no longer fires it.
      { id: 'old', patient_id: 'P1', insight_type: 'trajectory', severity: 'warning', title: 'SpO2 falling',
        detail: 'SpO2 trending down 96% to 91%.', state: 'active',
        created_at: '2026-08-07 06:00:00', updated_at: '2026-08-07 06:00:00' },
      // Not read off vitals, so a new set says nothing about it.
      { id: 'abx', patient_id: 'P1', insight_type: 'stewardship', severity: 'info', title: 'Antibiotic review due',
        detail: 'On ceftriaxone for >72h.', state: 'active',
        created_at: '2026-08-07 05:00:00', updated_at: '2026-08-07 05:00:00' },
    ]
    const c = card({ news2: news2({ raw_news2: 0, level: 'stable' }), last_obs_at: '2026-08-07 07:00:00' })
    const titles = currentIssues(c, insights).map(i => i.label)
    expect(titles).not.toContain('SpO2 falling')
    expect(titles).toContain('Antibiotic review due')
  })

  it('ignores dismissed insights and insights about other patients', () => {
    const insights: SageInsight[] = [
      { id: 'i1', patient_id: 'P1', insight_type: 'x', severity: 'critical', title: 'Dismissed one', state: 'dismissed', created_at: '2026-08-07 05:00:00' },
      { id: 'i2', patient_id: 'P9', insight_type: 'x', severity: 'critical', title: 'Other patient', state: 'new', created_at: '2026-08-07 05:00:00' },
    ]
    const c = card({ news2: news2({ raw_news2: 0, level: 'stable' }), last_obs_at: '2026-08-07 07:00:00' })
    expect(currentIssues(c, insights)).toHaveLength(0)
  })
})

describe('what needs deciding today', () => {
  it('lists an AI-extracted medication that no pharmacist has verified', () => {
    const c = card({
      medications: [{
        medication_id: 1, drug_name: 'Amoxicillin', dosage: '500mg', route: 'PO',
        frequency: 'TDS', schedule_kind: 'scheduled', interval_minutes: 480,
        status: 'active', last_given_at: null, given_count: 0, due_at: null,
        due_basis: 'order_start', missed_slots: 0, minutes_until_due: null,
        overdue: false, due_soon: false, unverified: true,
      }],
    })
    const decision = decisionsToday(c, [], true).find(d => d.source === 'medication')!
    expect(decision.label).toContain('Amoxicillin')
    expect(decision.detail).toMatch(/verif/i)
  })

  it('flags that no plan has been documented today', () => {
    const decisions = decisionsToday(card(), [], false)
    expect(decisions.some(d => d.source === 'documentation')).toBe(true)
  })

  it('stops flagging documentation once a plan exists for today', () => {
    const decisions = decisionsToday(card(), [], true)
    expect(decisions.some(d => d.source === 'documentation')).toBe(false)
  })

  it('offers a Sage suggestion as a decision, not as an instruction', () => {
    const insights: SageInsight[] = [{
      id: 'i1', patient_id: 'P1', insight_type: 'abx', severity: 'warning',
      title: 'Consider IV to oral switch', suggested_action_type: 'review',
      suggested_action_target: 'antibiotics', state: 'new', created_at: '2026-08-07 06:00:00',
    }]
    const decision = decisionsToday(card(), insights, true).find(d => d.source === 'sage')!
    expect(decision.label).toContain('IV to oral')
    // Provenance must travel with it — this is a suggestion from Sage.
    expect(decision.detail).toMatch(/sage/i)
  })

  it('never prints a raw action code (F031)', () => {
    const insights: SageInsight[] = [{
      id: 'i1', patient_id: 'P1', insight_type: 'abx', severity: 'warning',
      title: 'Consider IV to oral switch', suggested_action_type: 'order_review',
      suggested_action_target: 'antibiotics', state: 'new', created_at: '2026-08-07 06:00:00',
    }]
    const decision = decisionsToday(card(), insights, true).find(d => d.source === 'sage')!
    expect(decision.detail).toContain('Sage suggests: Order review antibiotics')
    expect(decision.detail).not.toContain('order_review')
  })
})

describe('documentation and day count', () => {
  it('counts a note written today as today plan', () => {
    expect(documentedToday([ev(at(2026, 7, 7, 8, 0), 'note', 'Ward round')], NOW)).toBe(true)
  })

  it('does not count yesterday note as today plan', () => {
    expect(documentedToday([ev(at(2026, 7, 6, 8, 0), 'note', 'Ward round')], NOW)).toBe(false)
  })

  it('ignores non-documentation events when deciding whether a plan exists', () => {
    // A dose given this morning is not a ward-round plan. Counting it would
    // silence the "no plan documented today" prompt on exactly the patients
    // who are being treated but not reviewed.
    expect(documentedToday([ev(at(2026, 7, 7, 8, 0), 'administration', 'Administered')], NOW)).toBe(false)
  })

  it('numbers the admission day from one', () => {
    expect(admissionDay(at(2026, 7, 7, 2, 0), NOW)).toBe(1)
    expect(admissionDay(at(2026, 7, 5, 2, 0), NOW)).toBe(3)
    expect(admissionDay(null, NOW)).toBeNull()
  })

  it('counts calendar days, not elapsed 24-hour blocks', () => {
    // Admitted at 23:00 last night, rounded on at 09:30 — that is day 2 on the
    // ward whiteboard, even though only ten hours have passed.
    expect(admissionDay(at(2026, 7, 6, 23, 0), NOW)).toBe(2)
  })
})
