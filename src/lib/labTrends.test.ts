/**
 * buildLabTrends carries a structured result's provenance (lis-5, part a).
 *
 * The honesty label: a result the simulator wrote (observations.source
 * 'simulated') must carry the "Simulated" badge on the chart's lab card, the
 * same as a simulated lab document already does (F044). Observation points
 * used to drop the source, so a simulated structured result showed as if it
 * were this patient's.
 *
 * Interfaced and imported rows are unchanged: same points, same status, no
 * label.
 */
import { describe, expect, it } from 'vitest'
import { buildLabTrends } from './labTrends'

const obs = (over: Record<string, unknown> = {}) => ({
  id: 1,
  observation_name: 'Potassium',
  value_numeric: 5.9,
  unit: 'mmol/L',
  reference_range: '3.5-5.1',
  abnormal_flag: 'H',
  status: 'final',
  source: 'ingested_hl7',
  observed_at: '2026-10-03 09:00:00',
  ...over,
})

describe('structured results carry their provenance', () => {
  it('labels a simulated observation as simulated', () => {
    const [trend] = buildLabTrends([], [obs({ source: 'simulated' })])
    expect(trend.name).toBe('Potassium')
    expect(trend.simulated).toBe(true)
  })

  it('leaves an interfaced observation unlabelled, with the same value and status as before', () => {
    const [trend] = buildLabTrends([], [obs()])
    expect(trend.simulated).toBe(false)
    expect(trend.current).toBe(5.9)
    expect(trend.status).toBe('high')
    expect(trend.refLow).toBe(3.5)
    expect(trend.refHigh).toBe(5.1)
  })

  it('leaves imported history and rows with no source unlabelled', () => {
    expect(buildLabTrends([], [obs({ source: 'history_import' })])[0].simulated).toBe(false)
    expect(buildLabTrends([], [obs({ source: undefined })])[0].simulated).toBe(false)
    expect(buildLabTrends([], [obs({ source: null })])[0].simulated).toBe(false)
  })

  it('labels the trend by its NEWEST point, so a genuine result after a simulated one is not labelled', () => {
    const [trend] = buildLabTrends([], [
      obs({ id: 1, source: 'simulated', value_numeric: 4.0, observed_at: '2026-10-03 06:00:00' }),
      obs({ id: 2, source: 'ingested_hl7', value_numeric: 5.9, observed_at: '2026-10-03 09:00:00' }),
    ])
    expect(trend.points).toEqual([4.0, 5.9])
    expect(trend.simulated).toBe(false)
  })

  it('labels the trend when the newest point is simulated', () => {
    const [trend] = buildLabTrends([], [
      obs({ id: 1, source: 'ingested_hl7', value_numeric: 4.0, observed_at: '2026-10-03 06:00:00' }),
      obs({ id: 2, source: 'simulated', value_numeric: 5.9, observed_at: '2026-10-03 09:00:00' }),
    ])
    expect(trend.simulated).toBe(true)
  })

  it('a simulated observation and a genuine document of one analyte stay one trend, labelled by the newest', () => {
    const doc = {
      doc_type: 'lab_result', source: null, created_at: '2026-10-03T10:00:00Z',
      content: '  Potassium: 6.1 mmol/L  HIGH  (ref: 3.5-5.1)',
    }
    const [trend] = buildLabTrends([doc], [obs({ source: 'simulated', observed_at: '2026-10-03 09:00:00' })])
    expect(trend.points).toEqual([5.9, 6.1])
    expect(trend.simulated).toBe(false)
  })
})

/* lis-5 (part b): a result Orb's own laboratory released (lab_result_id set,
   migration 56) takes its status from the laboratory's flag only. The range
   heuristic that calls a value past 1.5x the upper limit critical is never
   applied to it. Interfaced rows are unchanged. */
describe('a released laboratory result shows the laboratory\'s own flag', () => {
  const lis = (over: Record<string, unknown> = {}) => obs({ source: 'lis_manual', lab_result_id: 41, ...over })

  it('reads a potassium of 7.0 flagged HH as critical', () => {
    const [trend] = buildLabTrends([], [lis({ value_numeric: 7.0, abnormal_flag: 'HH' })])
    expect(trend.status).toBe('critical')
  })

  it('reads a creatinine of 2.0 flagged H as high, not critical', () => {
    const [trend] = buildLabTrends([], [lis({
      observation_name: 'Creatinine', unit: 'mg/dL', value_numeric: 2.0,
      reference_range: '0.6-1.2', abnormal_flag: 'H',
    })])
    expect(trend.status).toBe('high')
  })

  it('leaves an interfaced creatinine of 2.0 to the range heuristic, as before', () => {
    const [trend] = buildLabTrends([], [obs({
      observation_name: 'Creatinine', unit: 'mg/dL', value_numeric: 2.0,
      reference_range: '0.6-1.2', abnormal_flag: 'H',
    })])
    expect(trend.status).toBe('critical')
  })

  it('reads L, LL and A as the laboratory said', () => {
    expect(buildLabTrends([], [lis({ value_numeric: 3.0, abnormal_flag: 'L' })])[0].status).toBe('low')
    expect(buildLabTrends([], [lis({ value_numeric: 2.0, abnormal_flag: 'LL' })])[0].status).toBe('critical')
    expect(buildLabTrends([], [lis({ value_numeric: 4.0, abnormal_flag: 'A' })])[0].status).toBe('abnormal')
  })

  it('reads a blank flag as normal only when the laboratory had a range', () => {
    expect(buildLabTrends([], [lis({ value_numeric: 4.0, abnormal_flag: '' })])[0].status).toBe('normal')
    expect(buildLabTrends([], [lis({ value_numeric: 9.9, abnormal_flag: '', reference_range: null })])[0].status)
      .toBe('unranged')
    expect(buildLabTrends([], [lis({
      value_numeric: 9.9, abnormal_flag: '', reference_range: 'Reference range not configured',
    })])[0].status).toBe('unranged')
  })

  it('never lets the value outrank the flag: a value past the range with no flag is not high', () => {
    // The laboratory compared at its reporting precision against its own
    // approved range and raised nothing; the card does not second-guess it.
    expect(buildLabTrends([], [lis({ value_numeric: 5.15, abnormal_flag: '' })])[0].status).toBe('normal')
  })

  it('shows an unknown flag as abnormal, never as normal', () => {
    expect(buildLabTrends([], [lis({ value_numeric: 4.0, abnormal_flag: 'Q' })])[0].status).toBe('abnormal')
  })

  it('takes the status from the newest point, whichever lane it came from', () => {
    const [afterInterfaced] = buildLabTrends([], [
      obs({ id: 1, value_numeric: 9.0, abnormal_flag: 'H', observed_at: '2026-10-03 06:00:00' }),
      lis({ id: 2, value_numeric: 7.0, abnormal_flag: 'H', observed_at: '2026-10-03 09:00:00' }),
    ])
    expect(afterInterfaced.status).toBe('high')
    const [afterLis] = buildLabTrends([], [
      lis({ id: 1, value_numeric: 5.0, abnormal_flag: '', observed_at: '2026-10-03 06:00:00' }),
      obs({ id: 2, value_numeric: 9.0, abnormal_flag: 'H', observed_at: '2026-10-03 09:00:00' }),
    ])
    expect(afterLis.status).toBe('critical')
  })

  it('still labels a simulated laboratory row as simulated', () => {
    const [trend] = buildLabTrends([], [lis({ source: 'simulated', abnormal_flag: 'H' })])
    expect(trend.simulated).toBe(true)
    expect(trend.status).toBe('high')
  })

  it('treats a null lab_result_id as an interfaced row', () => {
    const [trend] = buildLabTrends([], [obs({ lab_result_id: null, value_numeric: 9.0, abnormal_flag: 'H' })])
    expect(trend.status).toBe('critical')
  })
})
