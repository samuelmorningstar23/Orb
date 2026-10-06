/**
 * THE TWO READS OF A NEWS2 RESULT THAT LIVE IN lib/news2.
 *
 * `breakdownRowFor` is how the Vigil command bar finds the row that scored one
 * of its five numbers. The bar used to colour those numbers with its own
 * cutoffs (HR > 100, SBP < 90, SpO2 < 94 ...), which disagreed with the NEWS2
 * breakdown printed directly under them: SBP 90 scores 3 and was left plain,
 * SBP 170 scores 0 and was painted amber. The only honest source for "does
 * this number count" is the row the engine wrote, matched by its exact label.
 *
 * `news2Description` printed "NEWS2 undefined (stable)" for the chart's
 * no-vitals risk, which has no `raw_news2` at all. Not recorded is the true
 * sentence, and it is the one the badge already uses for null.
 */
import { describe, expect, it } from 'vitest'
import { breakdownRowFor, news2Description } from './news2'
import type { News2 } from '../api/nurse'

/** A breakdown row in the server's shape (vigil_service.calculate_news2). */
const row = (parameter: string, value: unknown, points: number, note: string | null = null) =>
  ({ parameter, value, points, note, severe: false })

/* Unsorted on purpose: the server sorts worst first, the live tick does too,
   and nothing here may depend on either. */
const SCALE_1 = [
  row('Temperature', 37.1, 0),
  row('Systolic BP', 90, 3, 'Moderate hypotension (SBP 90)'),
  row('Consciousness (ACVPU)', 'confused', 3, 'Altered consciousness (ACVPU)'),
  row('Heart rate', 50, 1, 'Bradycardia (HR 50)'),
  row('SpO2', 95, 1, 'Mild hypoxia (SpO2 95%)'),
  row('Respiratory rate', 22, 2, 'Tachypnea (RR 22)'),
]

const SCALE_2 = [
  row('Supplemental O2', true, 2, 'On supplemental oxygen'),
  row('SpO2 (Scale 2)', 97, 3, 'Over-oxygenation on O2 (97%, Scale 2)'),
  row('Heart rate', 88, 0),
]

describe('breakdownRowFor finds the row that scored one bar number', () => {
  it('returns the row for each of the five vitals on Scale 1', () => {
    expect(breakdownRowFor(SCALE_1, 'heart_rate')).toEqual(row('Heart rate', 50, 1, 'Bradycardia (HR 50)'))
    expect(breakdownRowFor(SCALE_1, 'systolic_bp')?.points).toBe(3)
    expect(breakdownRowFor(SCALE_1, 'spo2')?.parameter).toBe('SpO2')
    expect(breakdownRowFor(SCALE_1, 'respiratory_rate')?.points).toBe(2)
    expect(breakdownRowFor(SCALE_1, 'temperature')?.value).toBe(37.1)
  })

  it('returns the Scale 2 SpO2 row for spo2, and never the oxygen row beside it', () => {
    const spo2 = breakdownRowFor(SCALE_2, 'spo2')
    expect(spo2?.parameter).toBe('SpO2 (Scale 2)')
    expect(spo2?.points).toBe(3)
  })

  it('returns a 0-point row: a measured vital in range is not an absent one', () => {
    const hr = breakdownRowFor(SCALE_2, 'heart_rate')
    expect(hr).not.toBeNull()
    expect(hr?.points).toBe(0)
  })

  it('returns null when the vital has no row (it was not measured)', () => {
    expect(breakdownRowFor(SCALE_2, 'temperature')).toBeNull()
    expect(breakdownRowFor(SCALE_2, 'systolic_bp')).toBeNull()
    expect(breakdownRowFor([], 'heart_rate')).toBeNull()
  })

  it('matches labels exactly: no case folding, no prefixes, no near misses', () => {
    const lookalikes = [
      row('heart rate', 120, 2),
      row('Heart rate (apical)', 120, 2),
      row('Systolic BP ', 85, 3),
      row('Diastolic BP', 40, 3),
      row('SpO2 (Scale 3)', 80, 3),
      row('SPO2', 80, 3),
      row('Supplemental O2', true, 2),
      row('Respiratory', 30, 3),
      row('Temp', 40, 2),
    ]
    for (const vital of ['heart_rate', 'systolic_bp', 'spo2', 'temperature', 'respiratory_rate'] as const) {
      expect(breakdownRowFor(lookalikes, vital)).toBeNull()
    }
  })

  it('never returns the consciousness row for any vital', () => {
    const only = [row('Consciousness (ACVPU)', 'voice', 3, 'Altered consciousness (ACVPU)')]
    for (const vital of ['heart_rate', 'systolic_bp', 'spo2', 'temperature', 'respiratory_rate'] as const) {
      expect(breakdownRowFor(only, vital)).toBeNull()
    }
  })

  it('returns null for a breakdown that is not an array', () => {
    for (const bad of [undefined, null, 'Heart rate', 3, { parameter: 'Heart rate', points: 2 }]) {
      expect(breakdownRowFor(bad, 'heart_rate')).toBeNull()
    }
  })

  it('skips entries that are not rows, and still finds the real one', () => {
    const messy = [null, 'Heart rate', 7, { points: 3 }, row('Heart rate', 135, 3, 'Severe tachycardia (HR 135)')]
    expect(breakdownRowFor(messy, 'heart_rate')?.value).toBe(135)
  })

  it('returns null when the matching row has no finite points', () => {
    for (const points of [undefined, null, '3', Number.NaN, Number.POSITIVE_INFINITY]) {
      const bad = [{ parameter: 'Heart rate', value: 135, points, note: null }]
      expect(breakdownRowFor(bad, 'heart_rate')).toBeNull()
    }
  })
})

const news2 = (over: Partial<News2> = {}): News2 => ({
  score: 0.3, level: 'elevated', raw_news2: 5, single_param_3: false, drivers: [],
  breakdown: [], scoring_model: 'NEWS2', spo2_scale: 1, ...over,
})

describe('news2Description', () => {
  it('says "not recorded" for the chart\'s no-vitals risk, which has no aggregate', () => {
    // GET /vigil/patients/{id} with no vitals on file returns exactly this.
    const noVitals = { score: 0, level: 'stable' as const, drivers: ['No vitals recorded'], scoring_model: 'NEWS2' }
    expect(news2Description(noVitals)).toBe('NEWS2 not recorded')
  })

  it('says "not recorded" for an aggregate that is not a finite number', () => {
    for (const raw of [Number.NaN, Number.POSITIVE_INFINITY, '5', null, undefined]) {
      expect(news2Description({ level: 'stable', raw_news2: raw as unknown as number })).toBe('NEWS2 not recorded')
    }
    expect(news2Description(null)).toBe('NEWS2 not recorded')
  })

  it('keeps every sentence it printed for a real score, including a score of 0', () => {
    expect(news2Description(news2({ raw_news2: 0, level: 'stable' }))).toBe('NEWS2 0 (stable)')
    expect(news2Description(news2({ raw_news2: 5, level: 'elevated' }))).toBe('NEWS2 5 (elevated)')
    expect(news2Description(news2({ raw_news2: 3, level: 'elevated', single_param_3: true, spo2_scale: 2 })))
      .toBe('NEWS2 3 (elevated) · a single parameter scores 3 · SpO₂ Scale 2')
  })

  /* An unmeasured parameter scores 0, so a NEWS2 from an incomplete set is a
     floor. Worded exactly as server/services/news2_text.py words it, so the
     badge and the Relay alert beside it say the same thing. */
  it('says an incomplete set is a minimum, last, and only when complete is false', () => {
    expect(news2Description(news2({ raw_news2: 6, level: 'elevated', complete: false })))
      .toBe('NEWS2 6 (elevated) · incomplete set, minimum')
    expect(news2Description(news2({
      raw_news2: 8, level: 'critical', single_param_3: true, spo2_scale: 2, complete: false,
    }))).toBe('NEWS2 8 (critical) · a single parameter scores 3 · SpO₂ Scale 2 · incomplete set, minimum')
    // true, or absent (an older payload), makes no coverage claim.
    for (const complete of [true, undefined]) {
      expect(news2Description(news2({ raw_news2: 6, level: 'elevated', complete })))
        .toBe('NEWS2 6 (elevated)')
    }
    // No aggregate is still "not recorded", whatever the coverage flag says.
    expect(news2Description({ level: 'stable', complete: false })).toBe('NEWS2 not recorded')
  })
})

/* The ward board row prints the two worst readings behind the score, taken
   from the breakdown the engine wrote, worst first by the Relay reason line's
   rule, each value as charted. */
describe('topReadings', () => {
  const br = (parameter: string, value: unknown, points: number, severe = false) =>
    ({ parameter, value, points, note: points ? 'x' : null, severe })

  it('names the two worst readings, worst first, values as charted', async () => {
    const { topReadings } = await import('./news2')
    expect(topReadings({
      raw_news2: 10,
      breakdown: [
        br('Respiratory rate', 36.5, 3, true),
        br('SpO2', 84, 3, true),
        br('Heart rate', 105, 1),
        br('Temperature', 38.05, 1),
      ],
    })).toEqual(['RR 37', 'SpO2 84%'])
  })

  it('puts a Severe 3 above a Moderate 3 and skips anything that scored 0', async () => {
    const { topReadings } = await import('./news2')
    expect(topReadings({
      raw_news2: 7,
      breakdown: [
        br('Respiratory rate', 26, 3, false),
        br('Systolic BP', 82, 3, true),
        br('Heart rate', 70, 0),
        br('Temperature', 39.5, 2),
      ],
    })).toEqual(['SBP 82', 'RR 26'])
    expect(topReadings({
      raw_news2: 2,
      breakdown: [br('Heart rate', 70, 0), br('Temperature', 38.05, 1), br('Supplemental O2', true, 2)],
    })).toEqual(['On oxygen', 'Temp 38.1°C'])
  })

  it('prints nothing for a score of 0, no result, or no breakdown', async () => {
    const { topReadings } = await import('./news2')
    expect(topReadings({ raw_news2: 0, breakdown: [br('Heart rate', 70, 0)] })).toEqual([])
    expect(topReadings(null)).toEqual([])
    expect(topReadings({ raw_news2: 4, breakdown: undefined as unknown as [] })).toEqual([])
  })

  it('says a new ACVPU change in words', async () => {
    const { topReadings } = await import('./news2')
    expect(topReadings({
      raw_news2: 3, breakdown: [br('Consciousness (ACVPU)', 'confusion', 3, true)],
    })).toEqual(['Confused'])
  })
})
