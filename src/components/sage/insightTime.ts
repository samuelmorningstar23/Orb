/**
 * Dating a Sage insight for the screen.
 *
 * Insight cards used to carry no time at all, so a card raised on vitals from
 * an hour ago read as the patient now and contradicted the live chart. The
 * server now rewrites an active insight's detail each time its rule refires
 * (sage_proactive_service._refresh_insight) and stamps `updated_at`, the time
 * the detail's numbers describe; `created_at` stays the time it was raised.
 */
import type { SageInsight } from '../../api/client'
import { formatUTCTime, parseUTC } from '../../lib/datetime'

/** The row as the server sends it (the shared type predates `updated_at`). */
export type DatedInsight = SageInsight & { updated_at?: string | null }

/** When the detail's numbers were read: `updated_at`, else the raise time. */
export function insightValuesAt(i: DatedInsight): string {
  return i.updated_at || i.created_at
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
