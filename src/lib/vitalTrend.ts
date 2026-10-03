/** The window behind the command-bar vital sparklines, and the words for it.
 *
 *  The sparkline used to be headed "TREND (6H)" with an axis from "-6h" to
 *  "now", while the data under it was simply the newest 40 entries of the
 *  chart's vitals array. That array grows two ways: the API returns up to 288
 *  rows, and the live socket prepends a point every 5 s. So on the simulator
 *  lane 40 points was about 200 seconds, and on a ward charting every 4 hours
 *  it was about a week. Either way the label described a span the data did
 *  not cover.
 *
 *  Now the window is chosen by TIME: at most the 6 hours that END AT THE NEWEST
 *  OBSERVATION (not at the wall clock, so a chart nobody has charted on lately
 *  still shows its last trend, labelled with when that was). The label then
 *  states the span those observations actually cover and the clock times of
 *  the first and last of them, because the window is often shorter than
 *  6 hours: the API's 288-row cap is 24 minutes on the 5-second lane.
 *
 *  Dense windows are thinned for drawing by keeping each time bucket's lowest
 *  and highest reading, so a spike survives where an average or a stride would
 *  drop it. The min and max on the axis come from every observation in the
 *  window, not from the thinned line.
 *
 *  Display only. Nothing here is stored, sent, or scored.
 */
import { parseUTC } from './datetime'

export const TREND_WINDOW_MS = 6 * 60 * 60 * 1000

/** Enough points for an 80 px wide line; more cannot be told apart. */
export const TREND_MAX_POINTS = 80

export interface TrendPoint {
  /** Epoch milliseconds of the observation. */
  t: number
  v: number
}

export interface VitalTrendWindow {
  /** Oldest first. Thinned to at most `maxPoints` when the window is dense. */
  points: TrendPoint[]
  /** Over every observation in the window, not only the drawn ones. */
  min: number
  max: number
  firstMs: number
  lastMs: number
  spanMs: number
  /** Observations in the window, before thinning. */
  count: number
}

/** A recorded reading, accepting a numeric string off the wire the way the
 *  cell's own formatter (src/lib/vitals.ts) does. Blank is absent, not 0. */
function reading(v: unknown): number | null {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null
  if (typeof v !== 'string' || v.trim() === '') return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

function observationTime(ts: unknown): number | null {
  // parseUTC('') returns NOW, which would plot an undated row as the newest
  // observation. A row without a timestamp has no place on a time axis.
  if (typeof ts !== 'string' || ts.trim() === '') return null
  const t = parseUTC(ts).getTime()
  return Number.isFinite(t) ? t : null
}

/** Keep each bucket's lowest and highest reading (plus the first and last
 *  observation, so the line spans the whole axis), in time order. */
function thin(points: TrendPoint[], maxPoints: number): TrendPoint[] {
  if (points.length <= maxPoints) return points
  const first = points[0]
  const last = points[points.length - 1]
  const span = last.t - first.t
  const buckets = Math.max(1, Math.floor((maxPoints - 2) / 2))
  const lo: (TrendPoint | undefined)[] = new Array(buckets)
  const hi: (TrendPoint | undefined)[] = new Array(buckets)
  for (const p of points) {
    const b = Math.min(buckets - 1, Math.floor(((p.t - first.t) / span) * buckets))
    if (!lo[b] || p.v < lo[b]!.v) lo[b] = p
    if (!hi[b] || p.v > hi[b]!.v) hi[b] = p
  }
  const keep = new Set<TrendPoint>([first, last])
  for (let b = 0; b < buckets; b++) {
    if (lo[b]) keep.add(lo[b]!)
    if (hi[b]) keep.add(hi[b]!)
  }
  // `points` is already in time order; filter preserves it, ties included.
  return points.filter(p => keep.has(p))
}

/** The observations of one vital that the sparkline may draw, or null when
 *  there is no trend to draw (fewer than two timed readings, or all at one
 *  instant). `vitals` may be in any order. */
export function vitalTrendWindow(
  vitals: readonly unknown[] | null | undefined,
  key: string,
  opts: { windowMs?: number; maxPoints?: number } = {},
): VitalTrendWindow | null {
  const windowMs = opts.windowMs ?? TREND_WINDOW_MS
  const maxPoints = Math.max(4, opts.maxPoints ?? TREND_MAX_POINTS)
  if (!vitals || vitals.length === 0) return null

  const timed: TrendPoint[] = []
  for (const item of vitals) {
    // A row is any record of readings; reading() and observationTime() reject
    // whatever is not a number or a timestamp.
    const row = item as Record<string, unknown> | null | undefined
    const v = reading(row?.[key])
    if (v === null) continue
    const t = observationTime(row?.timestamp)
    if (t === null) continue
    timed.push({ t, v })
  }
  if (timed.length < 2) return null

  timed.sort((a, b) => a.t - b.t)
  const lastMs = timed[timed.length - 1].t
  const inWindow = timed.filter(p => p.t >= lastMs - windowMs)
  if (inWindow.length < 2) return null
  const firstMs = inWindow[0].t
  const spanMs = lastMs - firstMs
  if (spanMs <= 0) return null

  let min = Infinity
  let max = -Infinity
  for (const p of inWindow) {
    if (p.v < min) min = p.v
    if (p.v > max) max = p.v
  }
  return {
    points: thin(inWindow, maxPoints),
    min, max, firstMs, lastMs, spanMs,
    count: inWindow.length,
  }
}

function spanParts(spanMs: number): { s?: number; h: number; m: number } {
  const seconds = Math.round(spanMs / 1000)
  if (seconds < 60) return { s: seconds, h: 0, m: 0 }
  const minutes = Math.round(spanMs / 60_000)
  return { h: Math.floor(minutes / 60), m: minutes % 60 }
}

/** For the tooltip header: "45 S", "5 MIN", "4H", "3H 10M". */
export function trendSpanShort(spanMs: number): string {
  const { s, h, m } = spanParts(spanMs)
  if (s !== undefined) return `${s} S`
  if (h === 0) return `${m} MIN`
  return m === 0 ? `${h}H` : `${h}H ${m}M`
}

/** For an aria-label: "45 seconds", "5 min", "4 hours", "3 hours 10 min". */
export function trendSpanSpoken(spanMs: number): string {
  const { s, h, m } = spanParts(spanMs)
  if (s !== undefined) return `${s} ${s === 1 ? 'second' : 'seconds'}`
  if (h === 0) return `${m} min`
  const hours = `${h} ${h === 1 ? 'hour' : 'hours'}`
  return m === 0 ? hours : `${hours} ${m} min`
}

function sameLocalDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
}

/** An axis end: the clock time in the app's "06:38 PM" form, and the date
 *  ("1 Oct") only when it is not today, so a stale trend cannot be read as
 *  this morning's. */
export function trendAxisLabel(ms: number, now: number = Date.now()): { date: string | null; time: string } {
  const d = new Date(ms)
  const time = d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })
  const date = sameLocalDay(d, new Date(now)) ? null : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })
  return { date, time }
}

/** The text after the colon in a vital cell's aria-label:
 *  "open trend, 5 min of observations, 11:54 AM to 11:59 AM". */
export function trendAriaPhrase(win: VitalTrendWindow, what: string = 'trend', now: number = Date.now()): string {
  const at = (ms: number) => {
    const { date, time } = trendAxisLabel(ms, now)
    return date ? `${date} ${time}` : time
  }
  return `open ${what}, ${trendSpanSpoken(win.spanMs)} of observations, ${at(win.firstMs)} to ${at(win.lastMs)}`
}
