/**
 * Orb, Laboratory validation API (lis-11, lis-12): delta reasons, technical
 * validation, authorisation and release, and sending a result back.
 *
 * TWO PEOPLE BY DEFAULT (ruling LIS-R5, held for Samuel). A signatory who
 * entered, changed, explained the delta of or technically validated a result
 * is refused 403 `second_person_required` for it while the site requires two
 * different people (the shipped default). The queue says so in advance on
 * each row (`may_authorise`, `you_took_part`), so the screen can offer only
 * what the server will accept; the server decides again on every call.
 *
 * EVERY STEP SAYS WHAT ITS SCREEN SHOWED (`expected`, see SeenResult in
 * ./results). A result changed since is refused 409 `result_changed` with no
 * value in the body: reload the queue, which is audited, to see it.
 *
 * DELTA CHECKS (ruling LIS-R9 (a)). A flagged change needs the
 * technologist's reason before technical validation (409
 * `delta_reason_required` otherwise). The server decides the check again when
 * the reason is given and at validation, so a reason given against one
 * earlier result never covers another. Timestamps are bare UTC.
 */
import { labGet, labPost } from './index'
import type { SeenResult } from './results'
import type { LabPriority } from './specimens'

/** What a delta check concluded. Only 'flagged' needs a reason. */
export type DeltaStatus =
  | 'flagged'
  | 'within'
  | 'no_previous'
  | 'outside_window'
  | 'bound'
  | 'previous_not_numeric'
  | 'unit_mismatch'
  | 'not_checked'

export interface DeltaCheck {
  status: DeltaStatus
  /** The check in words, written by the server. */
  note: string
  kind: 'absolute' | 'percent'
  threshold_text: string
  window_hours: number | null
  previous: {
    observation_id: number | null
    value_text: string | null
    unit: string | null
    /** Bare UTC, any spelling: format it through src/lib/datetime. */
    observed_at: string | null
    source: string | null
    from_laboratory: boolean
  } | null
  change_text: string | null
  percent_text: string | null
}

export interface DeltaReason {
  by_name: string | null
  at: string | null
  reason: string | null
}

/** The delta fields every result answer carries. */
export interface DeltaFields {
  delta: DeltaCheck | null
  needs_delta_reason: boolean
  delta_ack: DeltaReason | null
}

export interface ReturnedNote {
  by_name: string | null
  at: string | null
  reason: string | null
}

/** One result on the Validate screen. */
export interface QueueResult extends DeltaFields {
  result_id: number
  item_id: number
  order_id: string
  status: string
  test: { id: string; name: string; short_name: string; result_type: string }
  value_text: string | null
  comparator: string | null
  unit: string | null
  abnormal_flag: string
  reference_range_text: string | null
  comment: string | null
  is_critical: number
  critical_limit_text: string | null
  flag_text: string
  entered_by_name: string | null
  entered_at: string | null
  tech_validated_by_name: string | null
  tech_validated_at: string | null
  returned: ReturnedNote | null
  /** Authorisation rows only. */
  you_took_part?: boolean
  may_authorise?: boolean
}

export interface QueueGroup {
  specimen_id: string
  priority: LabPriority
  patient: { id: string; name: string; mrn: string | null; ward: string | null; bed: string | null }
  collected_at: string | null
  received_at: string | null
  results: QueueResult[]
}

export interface ValidationQueues {
  technical: QueueGroup[]
  authorisation: QueueGroup[]
  can_authorise: boolean
  require_distinct_authoriser: boolean
  scope: { department: string | null; hospital_wide: boolean }
}

export function getValidationQueues(): Promise<ValidationQueues> {
  return labGet<ValidationQueues>('/validation')
}

/** What a queue row's screen showed, for `expected`. */
export function seenOfQueue(result: QueueResult): SeenResult {
  return {
    value_text: result.value_text ?? '',
    comment: result.comment ?? null,
    abnormal_flag: result.abnormal_flag ?? null,
  }
}

export interface DeltaAckAnswer {
  result: Record<string, unknown> & DeltaFields
  /** False when the check, decided again, no longer flags. */
  acknowledged: boolean
  message?: string
}

export function acknowledgeDelta(resultId: number, reason: string, expected: SeenResult): Promise<DeltaAckAnswer> {
  return labPost<DeltaAckAnswer>(`/results/${encodeURIComponent(String(resultId))}/delta-ack`, {
    reason,
    expected,
  })
}

export interface ResultSeen {
  result_id: number
  expected: SeenResult
}

export function techValidate(results: ResultSeen[]): Promise<{ validated: number }> {
  return labPost<{ validated: number }>('/results/tech-validate', { results })
}

export interface ReleaseAnswer {
  specimen: { id: string; status: string; moved_to: string | null }
  released: number
  observation_ids: number[]
  orders: { id: string; status: string | null; conflict: boolean }[]
  /** How many of these results one person took part in and released (only
   *  when the site allows one signatory to do every step). */
  one_person: number
}

export function authoriseAndRelease(specimenId: string, results: ResultSeen[]): Promise<ReleaseAnswer> {
  return labPost<ReleaseAnswer>('/results/authorise', { specimen_id: specimenId, results })
}

export function returnToBench(resultId: number, reason: string, expected: SeenResult): Promise<{ result: unknown }> {
  return labPost<{ result: unknown }>(`/results/${encodeURIComponent(String(resultId))}/return`, {
    reason,
    expected,
  })
}

/** The delta fields of any result object the server sent, read defensively
 *  (the worklist's result type predates them). */
export function deltaFieldsOf(result: unknown): DeltaFields {
  const r = (result ?? {}) as Partial<DeltaFields>
  const delta = r.delta && typeof r.delta === 'object' && typeof r.delta.status === 'string'
    ? r.delta
    : null
  const ack = r.delta_ack && typeof r.delta_ack === 'object' ? r.delta_ack : null
  return { delta, needs_delta_reason: r.needs_delta_reason === true, delta_ack: ack }
}

/** A sent-back note on a result, read defensively, or null. */
export function returnedOf(result: unknown): ReturnedNote | null {
  const r = (result ?? {}) as { returned?: unknown }
  const note = r.returned
  if (!note || typeof note !== 'object') return null
  const n = note as Partial<ReturnedNote>
  return { by_name: n.by_name ?? null, at: n.at ?? null, reason: n.reason ?? null }
}
