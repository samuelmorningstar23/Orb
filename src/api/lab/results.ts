/**
 * Orb, Laboratory results API (lis-10): section worklists and manual result
 * entry.
 *
 * SEND THE VALUE AS TYPED ("5.10", "<0.01", "Reactive"), never a parsed
 * number. The server reads it at the test's reporting precision, refuses
 * more decimal places than the test reports (422 `precision`), keeps a
 * comparator as a bound with no H or L, refuses a coded value outside the
 * test's list (422 `not_in_list`, with `allowed`) and decides the flag.
 *
 * FLAGS come only from an approved reference range (rulings LIS-R2 and
 * LIS-R7). With none, or none for this patient's age and sex, the result
 * says why in `reference_range_text` and `flag_text`, and carries no flag.
 *
 * A worklist's `cancelled` list names who cancelled a test after its sample
 * was received, and when, so the bench stops. Timestamps are bare UTC.
 *
 * A CHANGE SAYS WHAT ITS SCREEN SHOWED (`expected`, see SeenResult): a
 * result someone else changed since is refused 409 `result_changed`.
 */
import { labGet, labPatch, labPost } from './index'
import type { LabPriority } from './specimens'

export interface WorklistSection {
  id: string
  name: string
  waiting: number
  entered: number
  stat_waiting: number
}

export interface WorklistTest {
  id: string
  name: string
  short_name: string
  result_type: 'numeric' | 'text' | 'coded'
  unit: string
  decimal_places: number
  coded_values: string[]
}

/** The range a value entered now would be flagged against. */
export interface WorklistRange {
  id: number | null
  /** "3.5 to 5.1 mmol/L", or why there is none. */
  text: string | null
  configured: boolean
}

export interface EnteredResult {
  id: number
  status: string
  version: number
  value_text: string | null
  comparator: string | null
  unit: string | null
  abnormal_flag: string
  reference_range_id: number | null
  reference_range_text: string | null
  comment: string | null
  entered_by_name: string | null
  entered_at: string | null
  /** The flag in words: "High", "Within range", "No flag: ...". */
  flag_text: string
}

export interface WorklistItem {
  item_id: number
  order_id: string
  specimen_id: string
  priority: LabPriority
  /** 'received' (waiting for a result) or 'resulted' (entered). */
  status: string
  patient: { id: string; name: string; mrn: string | null; ward: string | null; bed: string | null }
  test: WorklistTest
  received_at: string | null
  collected_at: string | null
  /** The turnaround target for this priority, or null when none is set. */
  tat_minutes: number | null
  range: WorklistRange
  result: EnteredResult | null
}

export interface CancelledTest {
  item_id: number
  order_id: string
  specimen_id: string
  test_name: string
  patient: { id: string; name: string; mrn: string | null }
  cancelled_by_name: string | null
  cancelled_at: string | null
  reason: string | null
}

export interface Worklist {
  section: { id: string; name: string }
  items: WorklistItem[]
  cancelled: CancelledTest[]
  scope: { department: string | null; hospital_wide: boolean }
}

/** A result as the server stores it, with its flag in words. */
export interface StoredResult extends EnteredResult {
  order_item_id: number
  test_id: string
  value_milli: number | null
  decimal_places: number | null
  [key: string]: unknown
}

export function listWorklists(): Promise<{ sections: WorklistSection[] }> {
  return labGet<{ sections: WorklistSection[] }>('/worklists')
}

export function getWorklist(sectionId: string): Promise<Worklist> {
  return labGet<Worklist>(`/worklists/${encodeURIComponent(sectionId)}`)
}

export function enterLabResult(
  orderItemId: number,
  value: string,
  comment?: string,
): Promise<{ result: StoredResult }> {
  return labPost<{ result: StoredResult }>('/results', {
    order_item_id: orderItemId,
    value,
    ...(comment ? { comment } : {}),
  })
}

/**
 * What the bench screen showed of a result. Every change sends it, and the
 * server refuses the change (409 `result_changed`, with the result as it now
 * stands in `current`) when the stored result no longer matches, so a screen
 * that is out of date never overwrites another person's change.
 */
export interface SeenResult {
  value_text: string
  comment: string | null
  abnormal_flag: string | null
}

/** The SeenResult for a result as the server last sent it. */
export function seenOf(result: EnteredResult): SeenResult {
  return {
    value_text: result.value_text ?? '',
    comment: result.comment ?? null,
    abnormal_flag: result.abnormal_flag ?? null,
  }
}

export function editLabResult(
  resultId: number,
  value: string,
  expected: SeenResult,
  extra: { comment?: string; reason?: string } = {},
): Promise<{ result: StoredResult; unchanged: boolean }> {
  return labPatch<{ result: StoredResult; unchanged: boolean }>(
    `/results/${encodeURIComponent(String(resultId))}`,
    { value, expected, ...extra },
  )
}

/** The allowed values from a 422 `not_in_list`, or null. */
export function allowedValues(detail: unknown): string[] | null {
  if (!detail || typeof detail !== 'object') return null
  const allowed = (detail as { allowed?: unknown }).allowed
  return Array.isArray(allowed) && allowed.every(v => typeof v === 'string') ? allowed : null
}
