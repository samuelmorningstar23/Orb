import { parseUTC } from './datetime'

/* The Vigil chart's lab-trend rules: how a trend arrow is toned and worded,
   and how recorded results become per-analyte trends. Pure, and kept out of
   VigilPanel.tsx so that file exports only components (fast refresh) and the
   tests can pin these rules directly. */

/* ─── The lab trend arrow: DIRECTION IS NOT A VERDICT ───
   PATIENT SAFETY. Seen on the running ward 2026-08-14, on two charts at once:

     * Priya Nair, admitted with DENGUE. PLATELETS 93 (ref 150–400), FALLING.
       Rendered ↓ in --status-ok. Falling platelets in dengue is the single
       thing that ward watches for, and the chart painted it as reassurance.
     * Lakshmi Devi. HAEMOGLOBIN 9.6 (ref 12–16), RISING back toward normal.
       Rendered ↑ in --status-warn — an amber warning for a patient getting
       better.

   The cause was one expression that coloured the arrow by DIRECTION ALONE:
   up = warn, down = ok. That is only ever right for analytes where high is the
   danger, and it is exactly inverted for the ones where low is (platelets,
   haemoglobin, sodium). "Up" and "down" carry no clinical sign.

   What does carry the sign is the REFERENCE RANGE, and it carries it for every
   analyte without this file needing to know which is which: measure how far the
   value sits OUTSIDE the range, then compare that distance now against the same
   distance one observation ago.

     moving TOWARD the range (distance shrinking)  → reassuring
     moving AWAY  from the range (distance growing) → concerning
     no change in distance, or no range to measure  → neither, say nothing

   Creatinine 1.8 → 2.1 (ref 0.7–1.3) grows its distance and reads concerning;
   platelets 120 → 93 (ref 150–400) grows its distance too, and finally reads
   concerning as well — the same rule, no per-analyte table.

   A value moving WITHIN the range has zero distance at both ends: neither
   reassuring nor concerning, so it is neutral and the value's own status colour
   is left to speak. Same for a lab with no reference range on file: the honest
   arrow is a grey one, never a green one. NEVER invert the meaning silently. */
export type LabTrendTone = 'reassuring' | 'concerning' | 'neutral'

/** How far `v` sits outside [lo, hi]. Zero anywhere inside the range. */
function distanceOutsideRange(v: number, lo: number, hi: number): number {
  if (v < lo) return lo - v
  if (v > hi) return v - hi
  return 0
}

/**
 * Tone for one lab's trend arrow, judged against its reference range.
 *
 * Reads the SAME last-two points the arrow glyph itself is derived from, so the
 * arrow and its colour can never describe different observations. Returns
 * 'neutral' — never a colour that reads as a verdict — whenever there is
 * nothing to compare (fewer than two points), nothing to compare against (no
 * usable reference range), or no change in distance from the range.
 */
export function labTrendTone(lab: {
  points?: unknown
  refLow?: unknown
  refHigh?: unknown
}): LabTrendTone {
  const pts = Array.isArray(lab.points) ? (lab.points as unknown[]) : []
  if (pts.length < 2) return 'neutral'
  const curr = pts[pts.length - 1]
  const prev = pts[pts.length - 2]
  const lo = lab.refLow
  const hi = lab.refHigh
  if (typeof curr !== 'number' || !Number.isFinite(curr)) return 'neutral'
  if (typeof prev !== 'number' || !Number.isFinite(prev)) return 'neutral'
  // No reference range on file (or a nonsensical one) — there is no direction
  // that is "better", so the arrow states movement and nothing more.
  if (typeof lo !== 'number' || !Number.isFinite(lo)) return 'neutral'
  if (typeof hi !== 'number' || !Number.isFinite(hi)) return 'neutral'
  if (hi < lo) return 'neutral'

  const before = distanceOutsideRange(prev, lo, hi)
  const now = distanceOutsideRange(curr, lo, hi)
  if (now < before) return 'reassuring'
  if (now > before) return 'concerning'
  return 'neutral'
}

/** Text colour per tone. `--status-x-text` (not the fill token) because this
 *  paints a glyph on a card surface — see the house contrast rule. */
export const LAB_TREND_TONE_COLOR: Record<LabTrendTone, string> = {
  reassuring: 'var(--status-ok-text)',
  concerning: 'var(--status-warn-text)',
  neutral: 'var(--text-muted)',
}

/** What the arrow means, in words. Colour is never the only carrier of a
 *  clinical judgement — a colour-blind nurse on a night shift reads this. */
export function labTrendLabel(
  tone: LabTrendTone,
  dir: unknown,
  lab: { refLow?: unknown; refHigh?: unknown },
): string {
  const movement = dir === 'up' ? 'rising' : dir === 'down' ? 'falling' : 'unchanged'
  const hasRange = typeof lab.refLow === 'number' && Number.isFinite(lab.refLow)
    && typeof lab.refHigh === 'number' && Number.isFinite(lab.refHigh)
  if (!hasRange) return `${movement}, no reference range on file`
  if (tone === 'reassuring') return `${movement} toward the reference range`
  if (tone === 'concerning') return `${movement} away from the reference range`
  return `${movement}, no change in distance from the reference range`
}

/* ─── Lab trends: RECORDED RESULTS ONLY ───
   The card used to fall back to an estimator that drew about twenty analyte
   values from the diagnosis whenever no lab document was on file, under a
   small "Estimated" chip the trend modal then dropped, and `dx.includes('mi')`
   put anaemia, hypoglycaemia, leukaemia and hyperkalaemia on an MI panel.
   Interfaced and imported results (the `observations` table) never reached the
   card at all, and the modal's x-axis assumed one result every six hours. Now
   the card reads lab_result documents and the structured observations the
   chart route returns, each point at the time it was observed, and says so
   when there is nothing to show. */
export type LabStatus = 'normal' | 'high' | 'low' | 'critical' | 'abnormal' | 'unranged'

export interface LabTrend {
  name: string
  unit: string
  refLow: number | null
  refHigh: number | null
  /** The reference range as recorded, for display when it is not two numbers. */
  refText: string | null
  /** Oldest first, parallel to `times`. */
  points: number[]
  times: string[]
  current: number
  status: LabStatus
  trendDir: 'up' | 'down' | 'stable'
  /** The newest result came from the simulator (source 'simulated'). */
  simulated: boolean
}

/* "  WBC: 12.5 10³/µL  HIGH  (ref: 4.0-11.0)": analyte, value, unit (one
   token, may be absent), then the range. The HIGH/LOW flag is skipped; status
   is recomputed from the value against the range. */
const LAB_DOC_LINE = /^\s*([A-Za-z0-9µ³/%\-\s]+?):\s*([\d.]+)\s+(?!HIGH\b|LOW\b|CRIT\b|CRITICAL\b)([^\s(]+)?[^(]*\(ref:\s*([\d.]+)\s*[-–]\s*([\d.]+)\)/i

function parseRange(text: unknown): [number, number] | null {
  if (typeof text !== 'string') return null
  const m = text.match(/^\s*(-?\d+(?:\.\d+)?)\s*[-–]\s*(-?\d+(?:\.\d+)?)\s*$/)
  if (!m) return null
  const lo = parseFloat(m[1]), hi = parseFloat(m[2])
  return Number.isFinite(lo) && Number.isFinite(hi) && hi >= lo ? [lo, hi] : null
}

function labStatus(v: number, lo: number | null, hi: number | null, flag: string | null): LabStatus {
  if (lo !== null && hi !== null) {
    if (v > hi) return v > hi * 1.5 ? 'critical' : 'high'
    if (v < lo) return v < lo * 0.7 ? 'critical' : 'low'
    return 'normal'
  }
  // No usable range: only the flag the laboratory sent can say anything.
  const f = (flag || '').trim().toUpperCase()
  if (f === 'HH' || f === 'LL' || f === 'AA') return 'critical'
  if (f === 'H' || f === '>') return 'high'
  if (f === 'L' || f === '<') return 'low'
  if (f === 'A') return 'abnormal'
  if (f === 'N') return 'normal'
  return 'unranged'
}

/* ─── A result Orb's own laboratory released: THE LABORATORY'S FLAG ONLY ───
   A row the laboratory module released (observations.lab_result_id is set)
   carries the flag the laboratory decided: H or L against a reference range a
   pathologist approved, HH or LL against an agreed critical limit, at the
   test's reporting precision. The card shows that verdict and never its own.
   The 1.5x / 0.7x range heuristic above is NEVER applied to these rows: a
   creatinine of 2.0 flagged H is high, not critical, because no critical limit
   said otherwise. A blank flag means within the range when the laboratory had
   one, and unranged when it did not ("Reference range not configured" is not
   a range, rulings LIS-R2 and LIS-R7). Interfaced and imported rows keep
   labStatus above, unchanged. */
function lisStatus(flag: string | null, refText: string | null): LabStatus {
  const f = (flag || '').trim().toUpperCase()
  if (f === 'HH' || f === 'LL' || f === 'AA') return 'critical'
  if (f === 'H') return 'high'
  if (f === 'L') return 'low'
  if (f === 'A') return 'abnormal'
  if (f === '' || f === 'N') {
    const ranged = refText !== null && refText.trim() !== '' && !/not configured/i.test(refText)
    return ranged ? 'normal' : 'unranged'
  }
  // A flag the laboratory vocabulary does not have is shown as abnormal, never
  // hidden as normal.
  return 'abnormal'
}

/** Every recorded numeric lab result, grouped per analyte and unit, oldest
 *  point first, each with the time it was observed. Pure, for the tests. */
export function buildLabTrends(notes: unknown, observations: unknown): LabTrend[] {
  type Point = { t: number; ts: string; v: number; flag: string | null; range: [number, number] | null; refText: string | null; simulated: boolean; lis: boolean }
  type Acc = { name: string; unit: string; pts: Point[] }
  const groups = new Map<string, Acc>()
  const add = (name: string, unit: string, range: [number, number] | null, refText: string | null,
    ts: unknown, v: unknown, flag: string | null, simulated = false, lis = false) => {
    if (typeof ts !== 'string' || !ts || typeof v !== 'number' || !Number.isFinite(v)) return
    const t = parseUTC(ts).getTime()
    if (!Number.isFinite(t)) return
    const key = `${name.trim().toLowerCase()}|${unit}`
    let a = groups.get(key)
    if (!a) {
      a = { name: name.trim(), unit, pts: [] }
      groups.set(key, a)
    }
    a.pts.push({ t, ts, v, flag, range, refText, simulated, lis })
  }

  for (const doc of Array.isArray(notes) ? notes : []) {
    if (doc?.doc_type !== 'lab_result' || typeof doc.content !== 'string') continue
    for (const line of doc.content.split('\n')) {
      const m = line.match(LAB_DOC_LINE)
      if (!m) continue
      const lo = parseFloat(m[4]), hi = parseFloat(m[5])
      add(m[1], (m[3] || '').trim(), Number.isFinite(lo) && Number.isFinite(hi) ? [lo, hi] : null,
        `${m[4]}-${m[5]}`, doc.created_at, parseFloat(m[2]), null, doc.source === 'simulated')
    }
  }
  for (const o of Array.isArray(observations) ? observations : []) {
    if (!o || typeof o.observation_name !== 'string') continue
    const refText = typeof o.reference_range === 'string' && o.reference_range.trim() ? o.reference_range.trim() : null
    /* Provenance rides along, as it does for lab documents above: a result the
       simulator wrote (source 'simulated') carries the honesty label on the
       card. It used to be dropped here, so a simulated structured result
       showed as if it were this patient's.
       A row Orb's laboratory released carries lab_result_id (migration 56);
       its status comes from the laboratory's own flag (lisStatus). */
    const lis = o.lab_result_id !== null && o.lab_result_id !== undefined && o.lab_result_id !== ''
    add(o.observation_name, (o.unit || '').trim(), parseRange(refText), refText,
      o.observed_at, typeof o.value_numeric === 'number' ? o.value_numeric : Number(o.value_numeric),
      typeof o.abnormal_flag === 'string' ? o.abnormal_flag : null, o.source === 'simulated', lis)
  }

  return [...groups.values()].map(a => {
    const pts = [...a.pts].sort((x, y) => x.t - y.t)
    const points = pts.map(p => p.v)
    const current = points[points.length - 1]
    const prev = points.length >= 2 ? points[points.length - 2] : current
    // The range on the newest result that carries one wins (a laboratory can
    // revise it), chosen by observed time, not by the order rows arrived in.
    const newestFirst = [...pts].reverse()
    const range = newestFirst.find(p => p.range)?.range ?? null
    const refLow = range ? range[0] : null
    const refHigh = range ? range[1] : null
    const newest = pts[pts.length - 1]
    return {
      name: a.name,
      unit: a.unit,
      refLow,
      refHigh,
      refText: newestFirst.find(p => p.refText)?.refText ?? null,
      points,
      times: pts.map(p => p.ts),
      current,
      status: newest.lis
        ? lisStatus(newest.flag, newest.refText)
        : labStatus(current, refLow, refHigh, newest.flag),
      trendDir: current > prev ? 'up' : current < prev ? 'down' : 'stable',
      simulated: pts[pts.length - 1].simulated,
    }
  })
}

/** The trend modal's series: each result at the time it was observed. */
export function labGraphData(lab: Pick<LabTrend, 'points' | 'times'>): { t: number; value: number }[] {
  return lab.points.map((value, i) => ({ t: parseUTC(lab.times[i]).getTime(), value }))
}
