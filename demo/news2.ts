/**
 * NEWS2 (RCP 2017) for the demo build: the same table as the server's
 * compute_news2, so the obs sheet's live preview scores what the real backend
 * would have. The shim answers /news2-preview with this instead of a recording.
 *
 * Its own module, not inside shim.ts, so it can be tested without loading the
 * shim (which patches fetch and boots a persona on import).
 *
 * SpO2 has two tables. Scale 2 is for COPD / chronic type-2 respiratory failure
 * (target 88-92%), chosen per patient by `on_scale_2`. This copy had no Scale 2
 * at all until 2026-10-02. Like the server, the breakdown row is named after
 * the table that scored it ("SpO2" or "SpO2 (Scale 2)") and the result carries
 * `spo2_scale`.
 */
import { chartedValue } from '../src/lib/vitals'

export interface DemoObs {
  respiratory_rate?: number | null
  spo2?: number | null
  on_oxygen?: boolean
  systolic_bp?: number | null
  heart_rate?: number | null
  temperature?: number | null
  consciousness?: string
}

// The RCP chart, top to bottom: the tie order for drivers, as on the server
// (`_RCP_CHART_ORDER` in server/services/vigil_service.py). Not the order the
// parameters are checked in below, which puts temperature before consciousness.
const CHART_ORDER: Record<string, number> = {
  'Respiratory rate': 0, SpO2: 1, 'SpO2 (Scale 2)': 1, 'Supplemental O2': 2,
  'Systolic BP': 3, 'Heart rate': 4, 'Consciousness (ACVPU)': 5, Temperature: 6,
}

export function news2(o: DemoObs, onScale2 = false) {
  const breakdown: { parameter: string; value: unknown; points: number; note: string | null; severe: boolean }[] = []
  const scored: { points: number; severe: boolean; rank: number; note: string }[] = []
  let score = 0, single3 = false
  // `severe`: set where the word Severe is chosen, and on an ACVPU change
  // (server rule, 2026-10-03). It outranks other entries with the same points.
  const add = (parameter: string, value: unknown, points: number, note: string, severe = false) => {
    score += points; if (points === 3) single3 = true
    if (points > 0 && note) scored.push({ points, severe, rank: CHART_ORDER[parameter] ?? 7, note })
    breakdown.push({ parameter, value, points, note: note || null, severe })
  }
  // AS CHARTED, the server's rule (server/services/vital_charting.py): every
  // band, word, label and breakdown value below comes from the vital rounded
  // half-up to the chart's precision, so RR 20.4 scores 0 and prints 20.
  const chart = (v: number | null | undefined, dp: 0 | 1 = 0) => (v == null ? v : chartedValue(v, dp))
  const rr = chart(o.respiratory_rate)
  if (rr != null) {
    if (rr <= 8) add('Respiratory rate', rr, 3, `Severe bradypnea (RR ${Math.round(rr)})`, true)
    else if (rr <= 11) add('Respiratory rate', rr, 1, `Low RR (${Math.round(rr)})`)
    else if (rr <= 20) add('Respiratory rate', rr, 0, '')
    else if (rr <= 24) add('Respiratory rate', rr, 2, `Tachypnea (RR ${Math.round(rr)})`)
    else add('Respiratory rate', rr, 3, `${rr >= 30 ? 'Severe' : 'Moderate'} tachypnea (RR ${Math.round(rr)})`, rr >= 30)
  }
  const sp = chart(o.spo2)
  if (sp != null) {
    const s = Math.round(sp)
    if (onScale2) {
      const p = 'SpO2 (Scale 2)'
      // Severity is relative to THIS patient's target range: severe below 84.
      if (sp <= 83) add(p, sp, 3, `${sp < 84 ? 'Severe' : 'Moderate'} hypoxia (SpO2 ${s}%, Scale 2)`, sp < 84)
      else if (sp <= 85) add(p, sp, 2, `Hypoxia (SpO2 ${s}%, Scale 2)`)
      else if (sp <= 87) add(p, sp, 1, `Low SpO2 (${s}%, Scale 2)`)
      else if (sp <= 92) add(p, sp, 0, '')                // target range
      else if (!o.on_oxygen) add(p, sp, 0, '')            // 93+ on air is fine
      else if (sp <= 94) add(p, sp, 1, `Over-target SpO2 on O2 (${s}%, Scale 2)`)
      else if (sp <= 96) add(p, sp, 2, `Over-target SpO2 on O2 (${s}%, Scale 2)`)
      else add(p, sp, 3, `Over-oxygenation on O2 (${s}%, Scale 2)`)
    } else {
      if (sp >= 96) add('SpO2', sp, 0, '')
      else if (sp >= 94) add('SpO2', sp, 1, `Mild hypoxia (SpO2 ${s}%)`)
      else if (sp >= 92) add('SpO2', sp, 2, `Hypoxia (SpO2 ${s}%)`)
      else add('SpO2', sp, 3, `${sp < 88 ? 'Severe' : 'Moderate'} hypoxia (SpO2 ${s}%)`, sp < 88)
    }
  }
  if (o.on_oxygen) add('Supplemental O2', true, 2, 'On supplemental oxygen')
  const sbp = chart(o.systolic_bp)
  if (sbp != null) {
    if (sbp <= 90) add('Systolic BP', sbp, 3, `${sbp < 80 ? 'Severe' : 'Moderate'} hypotension (SBP ${Math.round(sbp)})`, sbp < 80)
    else if (sbp <= 100) add('Systolic BP', sbp, 2, `Hypotension (SBP ${Math.round(sbp)})`)
    else if (sbp <= 110) add('Systolic BP', sbp, 1, `Low BP (SBP ${Math.round(sbp)})`)
    else if (sbp <= 219) add('Systolic BP', sbp, 0, '')
    else add('Systolic BP', sbp, 3, `Severe hypertension (SBP ${Math.round(sbp)})`, true)
  }
  const hr = chart(o.heart_rate)
  if (hr != null) {
    if (hr <= 40) add('Heart rate', hr, 3, `Severe bradycardia (HR ${Math.round(hr)})`, true)
    else if (hr <= 50) add('Heart rate', hr, 1, `Bradycardia (HR ${Math.round(hr)})`)
    else if (hr <= 90) add('Heart rate', hr, 0, '')
    else if (hr <= 110) add('Heart rate', hr, 1, `Mild tachycardia (HR ${Math.round(hr)})`)
    else if (hr <= 130) add('Heart rate', hr, 2, `Tachycardia (HR ${Math.round(hr)})`)
    else add('Heart rate', hr, 3, `${hr >= 140 ? 'Severe' : 'Moderate'} tachycardia (HR ${Math.round(hr)})`, hr >= 140)
  }
  const t = chart(o.temperature, 1)
  if (t != null) {
    if (t <= 35) add('Temperature', t, 3, `${t < 32 ? 'Severe hypothermia' : 'Hypothermia'} (${t.toFixed(1)}°C)`, t < 32)
    else if (t <= 36) add('Temperature', t, 1, `Low temp (${t.toFixed(1)}°C)`)
    else if (t <= 38) add('Temperature', t, 0, '')
    else if (t <= 39) add('Temperature', t, 1, `Fever (${t.toFixed(1)}°C)`)
    else add('Temperature', t, 2, `High fever (${t.toFixed(1)}°C)`)
  }
  const cons = String(o.consciousness || 'alert').trim().toLowerCase()
  if (!['alert', 'a', ''].includes(cons)) add('Consciousness (ACVPU)', cons, 3, 'Altered consciousness (ACVPU)', true)
  const level = score >= 7 ? 'critical' : score >= 5 || single3 ? 'elevated' : 'stable'
  const measured = { 'Respiratory rate': rr != null, SpO2: sp != null, 'Systolic BP': sbp != null, 'Heart rate': hr != null, Temperature: t != null, 'Consciousness (ACVPU)': !!o.consciousness, 'Supplemental O2': o.on_oxygen != null }
  const missing = Object.entries(measured).filter(([, v]) => !v).map(([k]) => k).sort()
  // The server's drivers (ruled 2026-10-02): worst first (points, then Severe
  // before Moderate since 2026-10-03), ties in chart order,
  // at most three, and a line saying so when nothing scored. Until then this
  // copy listed every driver in check order, so the preview pills disagreed
  // with the chart the moment the observation was saved.
  scored.sort((a, b) => b.points - a.points || Number(b.severe) - Number(a.severe) || a.rank - b.rank)
  const n = Object.keys(measured).length
  const drivers = scored.length ? scored.slice(0, 3).map(d => d.note)
    : [missing.length === 0 ? 'All vitals within normal range'
      : `Within range on what was measured (${n - missing.length}/${n} parameters)`]
  const band = score >= 7 ? ['NEWS2 7 or more', 30, 'Continuous monitoring. Emergency assessment by a critical-care-competent team.']
    : score >= 5 ? ['NEWS2 5-6', 60, 'Urgent review by a clinician competent in acute illness. Escalate now.']
    : single3 ? ['Single parameter 3', 60, 'A single parameter scoring 3 requires urgent review. Escalate.']
    : score >= 1 ? ['NEWS2 1-4', 240, 'Ward-based response: inform the registered nurse in charge.']
    : ['NEWS2 0', 720, 'Routine monitoring, minimum 12-hourly.']
  return {
    news2: { score: Math.min(1, score / 20), level, raw_news2: score, raw_mews: score, single_param_3: single3, drivers, complete: missing.length === 0, missing_parameters: missing, coverage_note: null, breakdown, spo2_scale: onScale2 ? 2 : 1 },
    schedule: { interval_minutes: band[1], band: band[0], response: band[2] },
  }
}
