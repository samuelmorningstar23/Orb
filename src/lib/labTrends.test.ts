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
