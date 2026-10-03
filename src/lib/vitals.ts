/** Display formatting for observations on documents a clinician reads or prints.
 *
 *  A blood pressure is read off a cuff to the nearest whole mmHg. A pulse and a
 *  respiratory rate are counted; you cannot count 62.3 breaths. SpO2 comes off
 *  the probe as a whole percent. Yet the handoff sheet and the downtime pack —
 *  the two surfaces that end up on PAPER, taped above a bed or carried between
 *  shifts — printed whatever float arrived on the wire, so a stored 110.8 was
 *  rendered "BP 110.8/62.3 mmHg".
 *
 *  That is not merely untidy. A decimal implies an instrument that measured to
 *  a tenth, so it reads as more precise than the observation actually was, and
 *  it costs a beat of reading time on a document whose entire purpose is to be
 *  scanned quickly. Temperature is the one vital genuinely charted to a tenth,
 *  and it keeps its decimal.
 *
 *  AS CHARTED (ruled 2026-10-02). NEWS2 is scored on the value rounded HALF-UP
 *  to the chart's precision (server/services/vital_charting.py), and its
 *  drivers print that number. These formatters print the same number, so a
 *  sheet cannot show "Temp 38.0°C" beside a driver reading "Fever (38.1°C)".
 *  Two things the obvious formatters get wrong for that: toFixed(1) rounds the
 *  BINARY value, and 38.05 is stored as 38.0499..., so it prints "38.0"; and
 *  Python's half-to-even rounding disagrees on 20.5. `chartedValue` rounds the
 *  shortest decimal form of the number, which is the value as entered.
 *
 *  Formatting, plus the one ordering rule at the bottom (which set is newer).
 *  Nothing here is stored or sent.
 */

import { parseUTC } from './datetime'

/** The em dash the clinical surfaces use for "not recorded". */
export const NO_VALUE = '—'

function asNumber(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? n : null
}

/** `n` rounded half-up (away from zero) to `dp` places, on its decimal form.
 *  The frontend twin of `as_charted` in server/services/vital_charting.py. */
export function chartedValue(n: number, dp: 0 | 1 = 0): number {
  const text = String(n)
  // Exponent notation only appears far outside any vital's range.
  if (/e/i.test(text)) return Number(n.toFixed(dp))
  // Shifting by editing the exponent is exact: "38.05e1" parses to 380.5.
  const shifted = Number(`${text}e${dp}`)
  const rounded = Math.sign(shifted) * Math.round(Math.abs(shifted))
  return Number(`${rounded}e-${dp}`)
}

/** A whole-number vital: BP, pulse, respiratory rate, SpO2. */
export function vitalWhole(v: unknown, dash: string = NO_VALUE): string {
  const n = asNumber(v)
  return n === null ? dash : String(chartedValue(n, 0))
}

/** Temperature: the one vital charted to a tenth of a degree. */
export function vitalTemperature(v: unknown, dash: string = NO_VALUE): string {
  const n = asNumber(v)
  return n === null ? dash : chartedValue(n, 1).toFixed(1)
}

/** `110/62`, with each half falling back to the dash independently. */
export function bloodPressure(
  systolic: unknown,
  diastolic: unknown,
  dash: string = NO_VALUE,
): string {
  return `${vitalWhole(systolic, dash)}/${vitalWhole(diastolic, dash)}`
}

/* ─── Which observation is newer ───
   Every surface that shows a vital set beside its NEWS2 also decides which set
   is "now", and a live tick can arrive older than the set already on screen (a
   back-charted reading, a second feed, a reload that resolves late). The Vigil
   chart and the ward board refuse a strictly older tick with this one rule, so
   the two cannot disagree about which set is current. */

/** Anything carrying an observation timestamp (a vitals row or a tick). */
export type Stamped = { timestamp?: unknown } | null | undefined

/** Epoch ms of an observation's timestamp, or null when it has none that
 *  parses. parseUTC rather than Date.parse: the vitals column holds both bare
 *  SQLite UTC strings and tz-aware ISO strings (see lib/datetime), and
 *  Date.parse reads a bare one as LOCAL time, which misorders a tick against a
 *  reloaded row by the viewer's UTC offset. parseUTC('') is "now", so an empty
 *  stamp is refused before it gets there. */
export function observedAt(v: Stamped): number | null {
  const ts = v?.timestamp
  if (typeof ts !== 'string' || ts === '') return null
  const ms = parseUTC(ts).getTime()
  return Number.isFinite(ms) ? ms : null
}

/** `a` was observed strictly before `b`. False whenever either stamp does not
 *  parse: a set we cannot date is never a reason to keep an older one. Equal
 *  is NOT older, because on a tie the later arrival is the later insert (the
 *  server breaks the same tie by id DESC). */
export function observedBefore(a: Stamped, b: Stamped): boolean {
  const ta = observedAt(a)
  const tb = observedAt(b)
  return ta !== null && tb !== null && ta < tb
}
