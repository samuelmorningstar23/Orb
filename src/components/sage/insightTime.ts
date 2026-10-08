/**
 * Dating a Sage insight for the screen.
 *
 * Insight cards used to carry no time at all, so a card raised on vitals from
 * an hour ago read as the patient now and contradicted the live chart. The
 * server now rewrites an active insight's detail each time its rule refires
 * (sage_proactive_service._refresh_insight); `created_at` stays the time it
 * was raised.
 *
 * `observed_at` (9 October 2026) is when the reading the detail describes was
 * taken: the vitals row's own time, for a set the ward clerk copied the time
 * on the paper sheet. It is what the card prints. `updated_at` is the sweep's
 * clock: printing it put "At 01:51 AM" on a set the sheet timed 22:50. It
 * stays only for a card that describes no reading (antibiotic review).
 */
import type { SageInsight } from '../../api/client'
import { formatUTCTime, parseUTC } from '../../lib/datetime'

/** The row as the server sends it (the shared type predates `updated_at`
 *  and `observed_at`). */
export type DatedInsight = SageInsight & { updated_at?: string | null; observed_at?: string | null }

/** When the detail's numbers were taken: `observed_at`; for a card with no
 *  reading behind it, the last time the server checked it (`updated_at`),
 *  else the raise time. */
export function insightValuesAt(i: DatedInsight): string {
  return i.observed_at || i.updated_at || i.created_at
}

/** "just now", "27 min ago", "3 h ago". '' for a missing or unreadable time. */
export function agoPhrase(ts: string | null | undefined, now: Date = new Date()): string {
  if (!ts) return ''
  const t = parseUTC(ts).getTime()
  if (isNaN(t)) return ''
  const min = Math.max(0, Math.floor((now.getTime() - t) / 60000))
  if (min < 1) return 'just now'
  if (min < 60) return `${min} min ago`
  return `${Math.floor(min / 60)} h ago`
}

/** "Raised 06:12 PM, 27 min ago". */
export function raisedLine(i: DatedInsight, now: Date = new Date()): string {
  const at = formatUTCTime(i.created_at)
  return at ? `Raised ${at}, ${agoPhrase(i.created_at, now)}` : ''
}

/** "At 06:39 PM: SpO2 trending down 94% to 88% over the last 3 readings." */
export function datedDetail(i: DatedInsight): string {
  if (!i.detail) return ''
  const at = formatUTCTime(insightValuesAt(i))
  return at ? `At ${at}: ${i.detail}` : i.detail
}
