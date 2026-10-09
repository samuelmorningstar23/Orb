/**
 * OPD vitals as plain words (opd-10, opd-11; rulings OPD-S3 and OPD-S6).
 *
 * What a consulting doctor types is kept inside the note with the time it was
 * measured. It is never scored, banded or coloured, so this file only turns
 * numbers into a line of text, through the chart's own formatters
 * (src/lib/vitals.ts) so a value reads the same here as on any chart.
 */
import type { OpdVitals } from '../../../api/opd'
import { bloodPressure, vitalTemperature, vitalWhole } from '../../../lib/vitals'

export type OpdVitalKey = keyof OpdVitals['values']

/** The vitals a consulting doctor may type, in the order they are written,
 *  with the plausible range the server accepts (a typo guard, not a clinical
 *  judgement: services/opd_service.OPD_VITAL_FIELDS). */
export const OPD_VITAL_INPUTS: { key: OpdVitalKey; label: string; unit: string; min: number; max: number;
  step: number }[] = [
  { key: 'heart_rate', label: 'Pulse', unit: 'per min', min: 1, max: 350, step: 1 },
  { key: 'systolic_bp', label: 'BP systolic', unit: 'mmHg', min: 1, max: 350, step: 1 },
  { key: 'diastolic_bp', label: 'BP diastolic', unit: 'mmHg', min: 1, max: 250, step: 1 },
  { key: 'respiratory_rate', label: 'Breathing', unit: 'per min', min: 1, max: 100, step: 1 },
  { key: 'spo2', label: 'SpO2', unit: '%', min: 1, max: 100, step: 1 },
  { key: 'temperature', label: 'Temperature', unit: '°C', min: 25, max: 46, step: 0.1 },
  { key: 'weight_kg', label: 'Weight', unit: 'kg', min: 0.2, max: 500, step: 0.1 },
  { key: 'height_cm', label: 'Height', unit: 'cm', min: 10, max: 260, step: 0.1 },
]

/** "Pulse 88 per min, BP 142/90 mmHg, Temperature 37.4 °C". */
export function opdVitalsLine(values: OpdVitals['values'] | null | undefined): string {
  const x = values ?? {}
  const parts: string[] = []
  if (x.heart_rate != null) parts.push(`Pulse ${vitalWhole(x.heart_rate)} per min`)
  if (x.systolic_bp != null || x.diastolic_bp != null) {
    parts.push(`BP ${bloodPressure(x.systolic_bp, x.diastolic_bp, '?')} mmHg`)
  }
  if (x.respiratory_rate != null) parts.push(`Breathing ${vitalWhole(x.respiratory_rate)} per min`)
  if (x.spo2 != null) parts.push(`SpO2 ${vitalWhole(x.spo2)}%`)
  if (x.temperature != null) parts.push(`Temperature ${vitalTemperature(x.temperature)} °C`)
  if (x.weight_kg != null) parts.push(`Weight ${x.weight_kg} kg`)
  if (x.height_cm != null) parts.push(`Height ${x.height_cm} cm`)
  return parts.join(', ')
}
