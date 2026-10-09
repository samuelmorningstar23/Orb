/**
 * Orb, Laboratory amended and withdrawn results API (lis-13).
 *
 * A REPORTED RESULT IS NEVER CHANGED IN PLACE. A signatory (lab.authorise)
 * amends it with a reason, which makes a new version and leaves the old one
 * on record, or withdraws a result entered in error. Either one tells the
 * ward in the patient's case room after it is saved.
 *
 * TWO SIGNATORIES BY DEFAULT (ruling LIS-R5, held). While the site requires
 * two different people, an amendment is PROPOSED by one signatory and applied
 * only when a different signatory confirms it; the chart keeps the reported
 * value until then. With the setting off, one signatory amends in one step.
 * A withdrawal is always one step: a value entered in error must not wait.
 *
 * EVERY STEP SAYS WHAT ITS SCREEN SHOWED (`expected`, see SeenResult in
 * ./results); a result changed since is refused 409 `result_changed`.
 */
import { labGet, labPost } from './index'
import type { SeenResult } from './results'
import type { CriticalDeliveryAnswer } from './criticals'

export interface ReportedVersion {
  result_id: number
  version: number
  status: 'released' | 'superseded' | 'withdrawn'
  value_text: string | null
  comparator: string | null
  unit: string | null
  abnormal_flag: string
  flag_text: string
  reference_range_text: string | null
  comment: string | null
  is_critical: number
  critical_limit_text: string | null
  entered_by_name: string | null
  entered_at: string | null
  released_by_name: string | null
  released_at: string | null
  amend_reason: string | null
}

export interface PendingAmendment {
  proposal_id: number
  proposed_by_name: string | null
  proposed_at: string | null
  value_text: string | null
  unit: string | null
  flag_text: string | null
  comment: string | null
  reason: string | null
  delta_reason: string | null
  you_proposed: boolean
  may_confirm: boolean
}

export interface ReportedTest {
  item_id: number
  order_id: string
  test: { id: string; name: string; short_name: string; result_type: string }
  /** The version on the chart, or null once withdrawn. */
  current: ReportedVersion | null
  /** Earlier versions it replaced, oldest first. */
  history: ReportedVersion[]
  withdrawn: ReportedVersion | null
  pending_amendment: PendingAmendment | null
}

export interface ReportedResults {
  specimen: {
    id: string
    status: string
    collected_at: string | null
    received_at: string | null
    patient: { id: string; name: string; mrn: string | null; ward: string | null; bed: string | null; discharged: boolean }
  }
  tests: ReportedTest[]
  can_amend: boolean
  require_distinct_authoriser: boolean
}

export function getReportedResults(specimenId: string): Promise<ReportedResults> {
  return labGet<ReportedResults>(`/results/reported/${encodeURIComponent(specimenId)}`)
}

/** What a reported version's row showed, for `expected`. */
export function seenOfReported(version: ReportedVersion): SeenResult {
  return {
    value_text: version.value_text ?? '',
    comment: version.comment ?? null,
    abnormal_flag: version.abnormal_flag ?? null,
  }
}

export interface WardNotice {
  posted: boolean
  reason: string | null
  message_id?: number | null
  channel_id?: string | null
}

export interface AmendAnswer {
  /** True once the new version is on the chart. */
  amended: boolean
  /** True when the amendment waits for a second signatory. */
  proposed: boolean
  proposal?: PendingAmendment | null
  ward_notice?: WardNotice
  criticals?: CriticalDeliveryAnswer[]
}

export interface AmendRequest {
  value: string
  reason: string
  comment?: string | null
  delta_reason?: string | null
}

export function amendResult(resultId: number, body: AmendRequest, expected: SeenResult): Promise<AmendAnswer> {
  return labPost<AmendAnswer>(`/results/${encodeURIComponent(String(resultId))}/amend`, {
    ...body,
    expected,
  })
}

export function confirmAmendment(resultId: number, proposal: PendingAmendment, expected: SeenResult): Promise<AmendAnswer> {
  return labPost<AmendAnswer>(`/results/${encodeURIComponent(String(resultId))}/amend/confirm`, {
    proposal_id: proposal.proposal_id,
    expected,
    proposed: { value_text: proposal.value_text ?? '', comment: proposal.comment ?? null },
  })
}

export function cancelAmendment(resultId: number, proposalId: number, reason: string): Promise<{ cancelled: boolean }> {
  return labPost<{ cancelled: boolean }>(`/results/${encodeURIComponent(String(resultId))}/amend/cancel`, {
    proposal_id: proposalId,
    reason,
  })
}

export interface WithdrawAnswer {
  order: { id: string; status: string | null; conflict: boolean }
  open_criticals: number
  incident_prompt: { incident_type: string; message: string } | null
  ward_notice: WardNotice
}

export function withdrawResult(resultId: number, reason: string, wrongPatient: boolean, expected: SeenResult): Promise<WithdrawAnswer> {
  return labPost<WithdrawAnswer>(`/results/${encodeURIComponent(String(resultId))}/withdraw`, {
    reason,
    wrong_patient: wrongPatient,
    expected,
  })
}

/** How the ward was told, in one sentence. */
export function noticeWords(notice: WardNotice | undefined): string {
  if (!notice) return ''
  if (notice.posted) return 'The ward was told in the patient\'s case room.'
  if (notice.reason === 'case_room_closed') {
    return 'The ward was not told in Relay: the patient has been discharged. Phone the discharging consultant.'
  }
  if (notice.reason === 'no_case_room') return 'The ward was not told in Relay: this patient has no case room. Phone the ward.'
  return 'The ward was not told in Relay: the notice failed. Phone the ward.'
}
