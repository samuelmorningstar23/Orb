/**
 * WARD ROUND QUEUE — the rules that decide which bed comes next.
 *
 * A round is not a list you scroll. It is a sequence Orb drives, it gets
 * interrupted constantly, and the patients you did NOT finish are the ones that
 * must be handed on. Four properties are asserted here because each of them, if
 * wrong, loses a patient:
 *
 *   1. BED ORDER IS PHYSICAL. The doctor walks the ward. "GM-10" comes after
 *      "GM-9", not between "GM-1" and "GM-2" — a plain string sort walks the
 *      ward in the wrong order and the doctor doubles back past ten beds.
 *   2. "NEXT" NEVER RE-OFFERS WHAT YOU JUST SKIPPED. Skip is how a doctor says
 *      "the patient is in radiology, come back". If Next hands the same bed
 *      straight back, Skip is unusable and the round stalls.
 *   3. RESUME IS THE DEFAULT. The round state survives leaving the tab and
 *      reloading the page, and it belongs to ONE clinician on ONE day — a ward
 *      tablet is shared, so another user's round must never be restored and
 *      must not be left sitting on the device.
 *   4. THE ROSTER MOVES UNDER YOU. Patients are admitted and discharged mid
 *      round. A new admission must appear as not-yet-seen; a discharged patient
 *      must leave the queue and stop counting toward "remaining".
 */
import { beforeEach, describe, expect, it } from 'vitest'
import type { NursePatientCard } from '../../api/nurse'
import type { Patient } from '../../api/client'
import {
  ROUND_STATE_PREFIX,
  buildQueue,
  compareBeds,
  handoffList,
  loadRoundState,
  mergeRoster,
  nextIncomplete,
  previousInQueue,
  progressOf,
  purgeForeignRoundState,
  reconcileStatuses,
  roundStateKey,
  saveRoundState,
  todayKey,
  type RoundPatient,
  type RoundStatus,
} from './roundQueue'

function patient(id: string, bed: string, over: Partial<RoundPatient> = {}): RoundPatient {
  return {
    id,
    name: `Patient ${id}`,
    bed,
    ward: 'GM',
    mrn: `MRN-${id}`,
    age: 60,
    sex: 'F',
    diagnosis: 'Pneumonia',
    admission_date: '2026-08-01 09:00:00',
    risk_score: null,
    risk_level: null,
    risk_drivers: [],
    attending_doctor_name: null,
    ...over,
  }
}

describe('bed order follows the walk, not the alphabet', () => {
  it('sorts numeric bed suffixes numerically', () => {
    const beds = ['GM-10', 'GM-2', 'GM-1', 'GM-9']
    expect([...beds].sort(compareBeds)).toEqual(['GM-1', 'GM-2', 'GM-9', 'GM-10'])
  })

  it('keeps different bays together and ordered', () => {
    const beds = ['ICU-3', 'GM-2', 'ICU-1', 'GM-11']
    expect([...beds].sort(compareBeds)).toEqual(['GM-2', 'GM-11', 'ICU-1', 'ICU-3'])
  })

  it('handles trailing-letter beds (4A, 4B, 12A) without crashing', () => {
    const beds = ['12A', '4B', '4A']
    expect([...beds].sort(compareBeds)).toEqual(['4A', '4B', '12A'])
  })

  it('puts patients with no bed recorded last instead of first', () => {
    // An empty string sorts before everything in a plain sort, which would open
    // the round on a patient who has no bed to walk to.
    const queue = buildQueue([patient('P3', ''), patient('P1', 'GM-2'), patient('P2', 'GM-1')])
    expect(queue.map(p => p.id)).toEqual(['P2', 'P1', 'P3'])
  })
})

describe('the queue advances and never re-offers a skip', () => {
  const queue = buildQueue([patient('A', 'GM-1'), patient('B', 'GM-2'), patient('C', 'GM-3')])

  it('moves to the next pending bed', () => {
    expect(nextIncomplete(queue, { A: 'seen' }, 'A')).toBe('B')
  })

  it('steps over a bed already seen', () => {
    expect(nextIncomplete(queue, { A: 'seen', B: 'seen' }, 'A')).toBe('C')
  })

  it('does not hand back the bed just skipped while pending beds remain', () => {
    // Skipping B must move to C, not sit on B.
    expect(nextIncomplete(queue, { A: 'seen', B: 'skipped' }, 'B')).toBe('C')
  })

  it('wraps to the start rather than dead-ending at the last bed', () => {
    expect(nextIncomplete(queue, { C: 'seen' }, 'C')).toBe('A')
  })

  it('only returns to skipped beds once nothing is pending', () => {
    // A and C seen, B skipped, standing on C — the round is not finished, and
    // the one unfinished patient is B.
    expect(nextIncomplete(queue, { A: 'seen', B: 'skipped', C: 'seen' }, 'C')).toBe('B')
  })

  it('returns null only when every bed has been seen', () => {
    expect(nextIncomplete(queue, { A: 'seen', B: 'seen', C: 'seen' }, 'C')).toBeNull()
  })

  it('reports no next bed when the only unfinished patient is the one in front of you', () => {
    // Standing at B, which you skipped, with A and C done. There is nowhere to
    // advance to — but the round is NOT complete, and the panel must say so
    // rather than congratulating the doctor on a patient they never saw.
    const statuses: Record<string, RoundStatus> = { A: 'seen', B: 'skipped', C: 'seen' }
    expect(nextIncomplete(queue, statuses, 'B')).toBeNull()
    expect(progressOf(queue, statuses).complete).toBe(false)
    expect(progressOf(queue, statuses).remaining).toBe(1)
  })

  it('steps backwards through the physical order regardless of status', () => {
    // Going back is a correction ("I mis-tapped"), so it ignores status.
    expect(previousInQueue(queue, 'B')).toBe('A')
    expect(previousInQueue(queue, 'A')).toBe('C')
  })
})

describe('progress and the hand-on list', () => {
  const queue = buildQueue([
    patient('A', 'GM-1'), patient('B', 'GM-2'), patient('C', 'GM-3'), patient('D', 'GM-4'),
  ])
  const statuses: Record<string, RoundStatus> = { A: 'seen', B: 'skipped', C: 'seen' }

  it('counts seen, skipped and not-yet-reached separately', () => {
    expect(progressOf(queue, statuses)).toEqual({
      total: 4, seen: 2, skipped: 1, pending: 1, remaining: 2, complete: false,
    })
  })

  it('is complete only when nothing is skipped or pending', () => {
    const done = progressOf(queue, { A: 'seen', B: 'seen', C: 'seen', D: 'seen' })
    expect(done.complete).toBe(true)
    expect(done.remaining).toBe(0)
  })

  it('hands on every patient not seen, skipped ones flagged', () => {
    const handoff = handoffList(queue, statuses)
    expect(handoff.map(h => [h.patient.id, h.status])).toEqual([
      ['B', 'skipped'],
      ['D', 'pending'],
    ])
  })
})

describe('a round survives being interrupted', () => {
  beforeEach(() => localStorage.clear())

  it('restores the cursor and the marks for the same clinician on the same day', () => {
    saveRoundState({
      userId: 'DOC1', dateKey: '2026-08-07', cursor: 'B', statuses: { A: 'seen' },
    })
    const restored = loadRoundState('DOC1', '2026-08-07')
    expect(restored?.cursor).toBe('B')
    expect(restored?.statuses).toEqual({ A: 'seen' })
  })

  it('does not restore another clinician round on a shared tablet', () => {
    saveRoundState({ userId: 'DOC1', dateKey: '2026-08-07', cursor: 'B', statuses: { A: 'seen' } })
    expect(loadRoundState('DOC2', '2026-08-07')).toBeNull()
  })

  it('does not restore yesterday round as if it were today', () => {
    saveRoundState({ userId: 'DOC1', dateKey: '2026-08-06', cursor: 'B', statuses: { A: 'seen' } })
    expect(loadRoundState('DOC1', '2026-08-07')).toBeNull()
  })

  it('wipes every other round off the device, not just ignores it', () => {
    // A ward tablet is shared. Leaving the previous doctor round on disk leaks
    // WHICH patients they saw, so it is erased rather than merely skipped.
    saveRoundState({ userId: 'DOC1', dateKey: '2026-08-07', cursor: 'B', statuses: { A: 'seen' } })
    saveRoundState({ userId: 'DOC2', dateKey: '2026-08-07', cursor: 'C', statuses: { C: 'seen' } })
    saveRoundState({ userId: 'DOC2', dateKey: '2026-08-01', cursor: 'A', statuses: {} })

    purgeForeignRoundState('DOC2', '2026-08-07')

    expect(localStorage.getItem(roundStateKey('DOC1', '2026-08-07'))).toBeNull()
    expect(localStorage.getItem(roundStateKey('DOC2', '2026-08-01'))).toBeNull()
    expect(loadRoundState('DOC2', '2026-08-07')?.cursor).toBe('C')

    const leftOver = Object.keys(localStorage).filter(k => k.startsWith(ROUND_STATE_PREFIX))
    expect(leftOver).toHaveLength(1)
  })

  it('survives corrupted storage instead of throwing on mount', () => {
    localStorage.setItem(roundStateKey('DOC1', '2026-08-07'), '{not json')
    expect(loadRoundState('DOC1', '2026-08-07')).toBeNull()
  })

  it('stores no patient names, beds or clinical text', () => {
    saveRoundState({ userId: 'DOC1', dateKey: '2026-08-07', cursor: 'B', statuses: { A: 'seen' } })
    const raw = localStorage.getItem(roundStateKey('DOC1', '2026-08-07')) ?? ''
    expect(raw).not.toMatch(/Patient|GM-|Pneumonia/)
  })

  it('produces a stable local date key', () => {
    expect(todayKey(new Date(2026, 7, 7, 6, 30))).toBe('2026-08-07')
  })
})

describe('the queue is built from two rosters, and survives losing one', () => {
  const wardCard = (id: string, bed: string) => ({
    patient_id: id, name: `Ward ${id}`, mrn: `M${id}`, bed, ward: 'GM',
    age: 70, sex: 'M', diagnosis: 'Sepsis', department: 'General Medicine',
    attending_doctor_name: 'Dr Rao', on_scale_2: false, allergies: [], news2: null,
    last_obs_at: null, obs_interval_minutes: 720, obs_band: 'low', obs_response: 'routine',
    obs_due_at: null, obs_minutes_until_due: null, obs_overdue: false, obs_due_soon: false,
    medications: [], meds_overdue: 0, meds_due_soon: 0,
  } as NursePatientCard)

  const vigilRow = (id: string, bed: string) => ({
    id, name: `Vigil ${id}`, mrn: `M${id}`, bed, ward: 'GM',
    admission_date: '2026-08-04 10:00:00', diagnosis: 'Sepsis', age: 70, sex: 'M',
    risk_score: 0.4, risk_level: 'elevated', risk_drivers: ['RR'],
  } as Patient)

  it('merges the two boards without duplicating a patient', () => {
    const queue = mergeRoster([wardCard('P1', 'GM-1')], [vigilRow('P1', 'GM-1')])
    expect(queue).toHaveLength(1)
    // Ward board wins on clinical identity; patient board supplies the fields
    // it alone carries.
    expect(queue[0].name).toBe('Ward P1')
    expect(queue[0].admission_date).toBe('2026-08-04 10:00:00')
    expect(queue[0].risk_level).toBe('elevated')
  })

  it('keeps a patient the ward board did not return', () => {
    // One request failing must never quietly delete a bed from the round.
    const queue = mergeRoster([], [vigilRow('P2', 'GM-2')])
    expect(queue.map(p => p.id)).toEqual(['P2'])
  })

  it('keeps a patient the patient board did not return', () => {
    const queue = mergeRoster([wardCard('P3', 'GM-3')], [])
    expect(queue.map(p => p.id)).toEqual(['P3'])
    expect(queue[0].admission_date).toBe('')
  })

  it('returns the merged queue already in walk order', () => {
    const queue = mergeRoster(
      [wardCard('B', 'GM-10'), wardCard('A', 'GM-2')],
      [vigilRow('C', 'GM-1')],
    )
    expect(queue.map(p => p.bed)).toEqual(['GM-1', 'GM-2', 'GM-10'])
  })
})

describe('the roster moves under the round', () => {
  it('drops marks for patients who left the ward mid round', () => {
    const queue = buildQueue([patient('A', 'GM-1'), patient('C', 'GM-3')])
    // B was discharged after being marked seen.
    const reconciled = reconcileStatuses(queue, { A: 'seen', B: 'seen', C: 'skipped' })
    expect(reconciled).toEqual({ A: 'seen', C: 'skipped' })
    expect(progressOf(queue, reconciled).total).toBe(2)
  })

  it('shows a patient admitted mid round as not yet seen', () => {
    const queue = buildQueue([patient('A', 'GM-1'), patient('NEW', 'GM-2')])
    const reconciled = reconcileStatuses(queue, { A: 'seen' })
    expect(reconciled.NEW).toBeUndefined()
    expect(progressOf(queue, reconciled)).toMatchObject({ pending: 1, complete: false })
    expect(nextIncomplete(queue, reconciled, 'A')).toBe('NEW')
  })
})
