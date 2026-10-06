import type { News2 } from '../api/nurse'
import { chartedValue } from './vitals'

/**
 * The one sentence a NEWS2 score means, in words.
 *
 * Lives in lib/ rather than beside the badge because three surfaces need the
 * SAME sentence and must not each write their own: the badge's `aria-label` and
 * `title`, and the deep-link rows on both homes, whose labels have to name the
 * score they are sending the reader to ("Anita Rao, bed GM-04, NEWS2 9
 * critical, 2 new results — open patient"). Two spellings of the same clinical
 * fact on one screen is how a reader learns to distrust both.
 *
 * The single-parameter-3 rule is spelled out because a patient can sit at an
 * aggregate of 3 and still need urgent review — the case most likely to be
 * missed by someone scanning a board for big numbers. The null case says "not
 * recorded" rather than nothing, because the visible em-dash announces as
 * silence and "no observation" is not "zero".
 *
 * The scoring fields are optional because the Vigil chart passes its risk
 * straight through, and with no vitals on file that risk has a level and no
 * aggregate at all. It used to print "NEWS2 undefined (stable)"; an aggregate
 * that is not a finite number is "not recorded", the same as null.
 *
 * An incomplete set is said to be a minimum (2026-10-03): an unmeasured
 * parameter scores 0, so the true NEWS2 can only be higher. The badge shows
 * "≥5" on the number; this is the same fact in words, worded exactly as the
 * server twin (server/services/news2_text.py) words it in every Relay alert,
 * handover and prompt. Only `complete === false` says so: a payload without
 * the field makes no claim either way.
 */
export type News2Summary = Pick<News2, 'level'> &
  Partial<Pick<News2, 'raw_news2' | 'single_param_3' | 'spo2_scale' | 'complete'>>

export function news2Description(news2: News2Summary | null): string {
  if (!news2 || !Number.isFinite(news2.raw_news2)) return 'NEWS2 not recorded'
  return (
    `NEWS2 ${news2.raw_news2} (${news2.level})` +
    (news2.single_param_3 ? ' · a single parameter scores 3' : '') +
    (news2.spo2_scale === 2 ? ' · SpO₂ Scale 2' : '') +
    (news2.complete === false ? ' · incomplete set, minimum' : '')
  )
}

/** One row of a NEWS2 breakdown, as vigil_service writes it: the parameter's
 *  label, its value as charted, the points it scored and the engine's note. */
export interface News2BreakdownRow {
  parameter: string
  value: unknown
  points: number
  note?: string | null
}

/** The five numbers in the Vigil command bar that NEWS2 scores. Diastolic is
 *  not one of them: the RCP table never scores it. */
export type News2ScoredVital = 'heart_rate' | 'systolic_bp' | 'spo2' | 'temperature' | 'respiratory_rate'

/* The engine's labels, EXACTLY. A substring or case-folded match would let
   "Supplemental O2" answer for SpO2, or "Diastolic BP" for the systolic, and
   the bar would colour a number by somebody else's points. */
const BREAKDOWN_LABELS: Record<News2ScoredVital, readonly string[]> = {
  heart_rate: ['Heart rate'],
  systolic_bp: ['Systolic BP'],
  spo2: ['SpO2', 'SpO2 (Scale 2)'],
  temperature: ['Temperature'],
  respiratory_rate: ['Respiratory rate'],
}

/**
 * The breakdown row that scored one command-bar vital, or null.
 *
 * Null is a real answer and must be read as "no cue", never as a reason to
 * fall back to a cutoff: the engine writes one row per MEASURED numeric vital
 * (0-point rows included), so no row means nobody measured it. A row whose
 * points are not a finite number is not trusted either.
 */
export function breakdownRowFor(breakdown: unknown, vital: News2ScoredVital): News2BreakdownRow | null {
  if (!Array.isArray(breakdown)) return null
  const labels = BREAKDOWN_LABELS[vital]
  if (!labels) return null
  for (const entry of breakdown) {
    if (!entry || typeof entry !== 'object') continue
    const parameter = (entry as { parameter?: unknown }).parameter
    if (typeof parameter !== 'string' || !labels.includes(parameter)) continue
    return Number.isFinite((entry as { points?: unknown }).points) ? (entry as News2BreakdownRow) : null
  }
  return null
}

/* ─── The readings behind a score, for a one-line board row ───
   The ward board row used to show a bare NEWS2 number. A nurse scanning the
   board wants to know WHY a bed is 9 without opening the chart, so the row now
   prints the two worst readings beside the badge, e.g. "RR 37 · SpO2 84%".

   Taken from the per-parameter breakdown the engine wrote (vigil_service
   compute_news2), never from cutoffs of our own: only a parameter that scored
   points is a reason. Worst first by the rule the Relay reason line uses
   (vigil_service.news2_reason_line): points, then Severe before Moderate, then
   RCP chart order. The breakdown arrives in chart order and Array.sort is
   stable, so sorting on the first two keys keeps the third. Values print as
   charted (half up, chartedValue), the number NEWS2 scored. */

/** Short label and unit per engine label. Exact labels, as breakdownRowFor. */
const READING_FORMAT: Record<string, { label: string; dp: 0 | 1; unit: string }> = {
  'Respiratory rate': { label: 'RR', dp: 0, unit: '' },
  SpO2: { label: 'SpO2', dp: 0, unit: '%' },
  'SpO2 (Scale 2)': { label: 'SpO2', dp: 0, unit: '%' },
  'Systolic BP': { label: 'SBP', dp: 0, unit: '' },
  'Heart rate': { label: 'HR', dp: 0, unit: '' },
  Temperature: { label: 'Temp', dp: 1, unit: '°C' },
}

/** ACVPU letters as a nurse charts them. */
const ACVPU_WORD: Record<string, string> = {
  c: 'Confused', confusion: 'Confused', confused: 'Confused', new_confusion: 'Confused',
  v: 'Responds to voice', voice: 'Responds to voice',
  p: 'Responds to pain', pain: 'Responds to pain',
  u: 'Unresponsive', unresponsive: 'Unresponsive',
}

function readingText(row: { parameter?: unknown; value?: unknown }): string | null {
  const parameter = typeof row.parameter === 'string' ? row.parameter : ''
  if (parameter === 'Supplemental O2') return 'On oxygen'
  if (parameter === 'Consciousness (ACVPU)') {
    const key = String(row.value ?? '').trim().toLowerCase()
    return ACVPU_WORD[key] ?? 'Not alert'
  }
  const fmt = READING_FORMAT[parameter]
  if (!fmt) return null
  const n = typeof row.value === 'number' ? row.value : Number(row.value)
  if (row.value === null || row.value === undefined || row.value === '' || !Number.isFinite(n)) return null
  const v = chartedValue(n, fmt.dp)
  return `${fmt.label} ${fmt.dp === 1 ? v.toFixed(1) : String(v)}${fmt.unit}`
}

/**
 * The worst readings behind a NEWS2 result, as short text, at most `limit`.
 * Empty for no result, a score of 0, or a breakdown with nothing that scored.
 */
export function topReadings(news2: Pick<News2, 'breakdown'> & Partial<Pick<News2, 'raw_news2'>> | null | undefined,
                            limit = 2): string[] {
  if (!news2 || !Array.isArray(news2.breakdown)) return []
  if (typeof news2.raw_news2 === 'number' && !(news2.raw_news2 > 0)) return []
  const scored = news2.breakdown.filter(b => b && Number.isFinite(b.points) && b.points > 0)
  scored.sort((a, b) => (b.points - a.points)
    || (Number(Boolean((b as { severe?: unknown }).severe)) - Number(Boolean((a as { severe?: unknown }).severe))))
  const out: string[] = []
  for (const row of scored) {
    if (out.length >= limit) break
    const text = readingText(row)
    if (text) out.push(text)
  }
  return out
}
