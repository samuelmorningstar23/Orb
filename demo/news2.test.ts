/**
 * The demo's copy of NEWS2 must score SpO2 on the patient's scale, exactly as
 * the server does. It is a COPY (the demo build has no backend), and it had
 * already drifted once: until 2026-10-02 it had no Scale 2 at all.
 *
 * Expected values are literals from the RCP NEWS2 chart, the same table the
 * server pins in server/tests/test_vigil_driver_labels.py, never computed with
 * the code under test.
 */
import { describe, expect, it } from 'vitest'
import { news2 } from './news2'

function spo2Row(spo2: number, onScale2: boolean, onOxygen = false) {
  const n = news2({ spo2, on_oxygen: onOxygen }, onScale2).news2
  const rows = n.breakdown.filter(b => b.parameter.startsWith('SpO2'))
  expect(rows).toHaveLength(1)
  return { ...rows[0], scale: n.spo2_scale }
}

// [charted SpO2, points]
const SCALE_1: [number, number][] = [[80, 3], [91, 3], [92, 2], [93, 2], [94, 1], [95, 1], [96, 0], [100, 0]]
const SCALE_2_ON_AIR: [number, number][] = [
  [80, 3], [83, 3], [84, 2], [85, 2], [86, 1], [87, 1], [88, 0], [92, 0], [93, 0], [97, 0], [100, 0]]
const SCALE_2_ON_O2: [number, number][] = [
  [83, 3], [84, 2], [85, 2], [86, 1], [87, 1], [88, 0], [92, 0], [93, 1], [94, 1], [95, 2], [96, 2], [97, 3], [100, 3]]

describe('demo NEWS2: SpO2 follows the patient\'s scale', () => {
  it.each(SCALE_1)('Scale 1: SpO2 %i scores %i', (spo2, points) => {
    expect(spo2Row(spo2, false)).toMatchObject({ parameter: 'SpO2', points, scale: 1 })
  })

  it.each(SCALE_2_ON_AIR)('Scale 2 on air: SpO2 %i scores %i', (spo2, points) => {
    expect(spo2Row(spo2, true)).toMatchObject({ parameter: 'SpO2 (Scale 2)', points, scale: 2 })
  })

  it.each(SCALE_2_ON_O2)('Scale 2 on O2: SpO2 %i scores %i', (spo2, points) => {
    expect(spo2Row(spo2, true, true)).toMatchObject({ parameter: 'SpO2 (Scale 2)', points, scale: 2 })
  })

  it('words Scale 2 the way the server does', () => {
    expect(spo2Row(83, true).note).toBe('Severe hypoxia (SpO2 83%, Scale 2)')
    expect(spo2Row(85, true).note).toBe('Hypoxia (SpO2 85%, Scale 2)')
    expect(spo2Row(87, true).note).toBe('Low SpO2 (87%, Scale 2)')
    expect(spo2Row(94, true, true).note).toBe('Over-target SpO2 on O2 (94%, Scale 2)')
    expect(spo2Row(97, true, true).note).toBe('Over-oxygenation on O2 (97%, Scale 2)')
    expect(spo2Row(90, true).note).toBeNull()
    // Scale 1 wording is unchanged by the move out of shim.ts.
    expect(spo2Row(88, false).note).toBe('Moderate hypoxia (SpO2 88%)')
    expect(spo2Row(87, false).note).toBe('Severe hypoxia (SpO2 87%)')
  })

  it('scores the charted value: SpO2 87.5 is charted 88, on target on Scale 2', () => {
    expect(spo2Row(87.5, true)).toMatchObject({ value: 88, points: 0 })
  })

  it('lists drivers the way the server does: worst first, chart order, three at most', () => {
    // Literals from server/tests/test_news2_driver_order.py.
    const drivers = (o: Parameters<typeof news2>[0], s2 = false) => news2(o, s2).news2.drivers
    const normal = { respiratory_rate: 16, spo2: 97, on_oxygen: false, systolic_bp: 120,
                     heart_rate: 80, temperature: 37.0, consciousness: 'alert' }
    expect(drivers({ ...normal, respiratory_rate: 10, spo2: 95, heart_rate: 105, consciousness: 'confusion' }))
      .toEqual(['Altered consciousness (ACVPU)', 'Low RR (10)', 'Mild hypoxia (SpO2 95%)'])
    expect(drivers({ ...normal, respiratory_rate: 7, temperature: 34.8, consciousness: 'confusion' }))
      .toEqual(['Severe bradypnea (RR 7)', 'Altered consciousness (ACVPU)', 'Hypothermia (34.8°C)'])
    expect(drivers({ ...normal, spo2: 86, on_oxygen: true }, true))
      .toEqual(['On supplemental oxygen', 'Low SpO2 (86%, Scale 2)'])
    // Severe before Moderate among 3-pointers (2026-10-03): septic shock, hypothermia.
    expect(drivers({ ...normal, heart_rate: 142, systolic_bp: 82, respiratory_rate: 26, temperature: 39.5, spo2: 90 }))
      .toEqual(['Severe tachycardia (HR 142)', 'Moderate tachypnea (RR 26)', 'Moderate hypoxia (SpO2 90%)'])
    expect(drivers({ ...normal, heart_rate: 38, systolic_bp: 82, respiratory_rate: 8, temperature: 31.6, spo2: 90, consciousness: 'voice' }))
      .toEqual(['Severe bradypnea (RR 8)', 'Severe bradycardia (HR 38)', 'Altered consciousness (ACVPU)'])
    expect(drivers(normal)).toEqual(['All vitals within normal range'])
    expect(drivers({ heart_rate: 80 })).toEqual(['Within range on what was measured (1/7 parameters)'])
  })

  it('pages an over-oxygenated COPD patient that Scale 1 would call stable', () => {
    const obs = { respiratory_rate: 16, spo2: 97, on_oxygen: true, systolic_bp: 124,
                  heart_rate: 81, temperature: 37.1, consciousness: 'alert' }
    expect(news2(obs, false).news2).toMatchObject({ raw_news2: 2, level: 'stable' })
    expect(news2(obs, true).news2).toMatchObject({ raw_news2: 5, level: 'elevated', spo2_scale: 2 })
  })
})
