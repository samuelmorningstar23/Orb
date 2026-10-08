/**
 * Sage insight cards carry when they were raised and when their numbers were
 * read (F031). Undated, a card raised on an hour-old set read as the patient
 * now and contradicted the live chart.
 */
import { describe, expect, it } from 'vitest'
import { formatUTCTime } from '../../lib/datetime'
import { agoPhrase, datedDetail, raisedLine, type DatedInsight } from './insightTime'

const insight = (over: Partial<DatedInsight> = {}): DatedInsight => ({
  id: 'i1', patient_id: 'P1', insight_type: 'trajectory', severity: 'warning',
  title: 'SpO2 falling', detail: 'SpO2 trending down 96% to 91% over the last 3 readings.',
  state: 'active', created_at: '2026-08-07 06:00:00', ...over,
})

describe('insight times', () => {
  it('says when the card was raised and how long ago', () => {
    const now = new Date(Date.UTC(2026, 7, 7, 6, 27))
    expect(raisedLine(insight(), now)).toBe(`Raised ${formatUTCTime('2026-08-07 06:00:00')}, 27 min ago`)
  })

  it('dates the detail with the refresh time, not the raise time', () => {
    const line = datedDetail(insight({ updated_at: '2026-08-07T06:40:00+00:00' }))
    expect(line).toBe(`At ${formatUTCTime('2026-08-07T06:40:00+00:00')}: SpO2 trending down 96% to 91% over the last 3 readings.`)
  })

  it('dates the detail with the time the reading was taken, not the sweep time', () => {
    // A set copied in at 01:51 from a sheet that times it 22:50.
    const line = datedDetail(insight({
      observed_at: '2026-10-08T22:50:00+00:00', updated_at: '2026-10-09T01:51:00+00:00',
    }))
    expect(line).toBe(`At ${formatUTCTime('2026-10-08T22:50:00+00:00')}: SpO2 trending down 96% to 91% over the last 3 readings.`)
    expect(line).not.toContain(formatUTCTime('2026-10-09T01:51:00+00:00'))
  })

  it('falls back to the raise time for a row with no refresh time', () => {
    expect(datedDetail(insight())).toMatch(new RegExp(`^At ${formatUTCTime('2026-08-07 06:00:00')}: `))
  })

  it('reads a bare database time as UTC', () => {
    const now = new Date(Date.UTC(2026, 7, 7, 8, 5))
    expect(agoPhrase('2026-08-07 06:00:00', now)).toBe('2 h ago')
    expect(agoPhrase('2026-08-07 08:04:30', now)).toBe('just now')
    expect(agoPhrase(null, now)).toBe('')
  })
})
