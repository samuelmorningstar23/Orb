/**
 * Orb, Laboratory critical results API (lis-15): the laboratory's Critical
 * results screen and its callback log.
 *
 * A CRITICAL IS RAISED BY THE LABORATORY'S OWN STEPS (technical validation,
 * release, an amendment) against a limit a pathologist approved; nothing here
 * raises one. After the step commits, Orb posts it to the patient's case room
 * as "Laboratory". A post that could not be delivered (no case room, a
 * discharged patient's closed room, a failed write) leaves the critical open
 * and due at once.
 *
 * A VALUE THAT CHANGES. When a result is sent back to the bench and validated
 * again with a different value, the critical says so: `value_told` is the
 * value the ward was last sent, `value_now` the result as it now stands, and
 * `changes` the record. The ward is told the change in the case room; a
 * critical already closed whose value is no longer critical is reopened, so
 * the laboratory phones the correction and logs it.
 *
 * WHAT CLOSES ONE (ruling LIS-R3): only a callback logged here with the name
 * and role of the person told, how they were told, and their read-back. A
 * Relay post or reply never closes it, and neither does discharge or a
 * transfer. A call that reached nobody is logged as an attempt and keeps it
 * open. A callback and an attempt each name the `value_told` the screen
 * showed; when the value changed after the screen loaded the server refuses
 * it 409 'value_changed' and writes nothing, so the screen reloads.
 *
 * The list read is audited on the server, so a screen loads it when it opens,
 * after each step, on request and on a slow timer. Timestamps are bare UTC:
 * format them through src/lib/datetime.
 */
import { labGet, labPost } from './index'

/** How a critical's case-room post stands. */
export type CriticalDelivery = 'pending' | 'posted' | 'no_case_room' | 'failed'

export interface CriticalCallback {
  at: string | null
  by_name: string | null
  recipient_name: string | null
  recipient_role: string | null
  recipient_role_text: string | null
  method: string | null
  method_text: string | null
  read_back: boolean
  /** False for a call that reached nobody. */
  reached: boolean
  note: string | null
}

/** One recorded change of the value a critical stands for. */
export interface CriticalValueChange {
  at: string
  by_name: string | null
  from_value: string | null
  to_value: string | null
  still_critical: boolean
  /** The critical had been closed and was reopened by the change. */
  reopened: boolean
}

export interface CriticalNotification {
  id: string
  status: 'open' | 'acknowledged' | 'closed_unreachable'
  source_lane: 'lis' | 'interfaced'
  patient: {
    id: string
    name: string | null
    mrn: string | null
    /** Null for a discharged patient: the bed is someone else's now. */
    ward: string | null
    bed: string | null
    discharged: boolean
    discharged_at: string | null
  }
  test_name: string
  value_display: string
  critical_limit_text: string | null
  /** Raised before authorisation (ruling LIS-R4 (a)). */
  preliminary: boolean
  result_id: number | null
  /** The value the ward was last sent for this critical: the value a
   *  callback reads back, and the one it names. */
  value_told: string
  /** The result's value as it now stands; null while it is back on the bench or withdrawn. */
  value_now?: string | null
  /** What has happened to the result since: the server's words. `changed`
   *  says the value the ward was first given no longer stands as it was. */
  result_now: { status: string | null; text: string | null; changed?: boolean }
  changes?: CriticalValueChange[]
  raised_at: string
  raised_by_name: string | null
  raised_in_department: string | null
  tier: {
    current: number
    count: number
    label: string | null
    next_label: string | null
    ladder_configured: boolean
    due_at: string | null
    due_now: boolean
  }
  delivery: {
    status: CriticalDelivery
    reason: string | null
    /** The server's words, for the screen. */
    text: string
    message_id: number | null
  }
  callbacks: CriticalCallback[]
  acknowledged: (Omit<CriticalCallback, 'reached' | 'note'> & { at: string | null }) | null
  events: { type: string; tier: number | null; at: string; detail: string | null; by_name: string | null }[]
}

export interface CriticalLadderTier {
  tier: number
  role: string
  label: string
  timeout_minutes: number | null
}

export interface CriticalList {
  open: CriticalNotification[]
  recent: CriticalNotification[]
  ladder: { configured: boolean; tiers: CriticalLadderTier[]; source: string | null }
  /** The roles a callback may name, with their words. */
  recipient_roles: Record<string, string>
  methods: Record<string, string>
  recent_hours: number
  /** Whether this user may log a callback (lab.critical_log). */
  can_log: boolean
  scope: { department: string | null; hospital_wide: boolean }
}

export function getCriticals(): Promise<CriticalList> {
  return labGet<CriticalList>('/criticals')
}

export interface CallbackRequest {
  recipient_name: string
  recipient_role: string
  method: 'phone' | 'in_person'
  /** Must be true: the server refuses a callback without a read-back. */
  read_back: boolean
  note?: string | null
  /** The value the screen showed as told to the ward (the notification's
   *  `value_told`). Refused 409 'value_changed' when it no longer stands. */
  value_told: string
}

export function logCriticalCallback(
  notificationId: string, body: CallbackRequest,
): Promise<{ notification: CriticalNotification }> {
  return labPost<{ notification: CriticalNotification }>(
    `/criticals/${encodeURIComponent(notificationId)}/callback`, body)
}

export function logCriticalAttempt(
  notificationId: string, body: { method: 'phone' | 'in_person'; note: string; value_told: string },
): Promise<{ notification: CriticalNotification }> {
  return labPost<{ notification: CriticalNotification }>(
    `/criticals/${encodeURIComponent(notificationId)}/attempt`, body)
}

/** One critical's delivery after a validation, release or amendment. */
export interface CriticalDeliveryAnswer {
  notification_id: string
  posted: boolean
  reason: string | null
  message_id: number | null
  /** What the post told the ward: a new critical ('raised'), a critical
   *  whose value changed and is still critical ('changed'), or a correction:
   *  the value given earlier is no longer critical ('corrected'). */
  kind?: 'raised' | 'changed' | 'corrected' | null
}

function isChange(answer: CriticalDeliveryAnswer): boolean {
  return answer.kind === 'changed' || answer.kind === 'corrected'
}

/** The `criticals` list a laboratory step answers with, read defensively. */
export function criticalsOf(answer: unknown): CriticalDeliveryAnswer[] {
  const list = (answer ?? {}) as { criticals?: unknown }
  if (!Array.isArray(list.criticals)) return []
  return list.criticals.filter((c): c is CriticalDeliveryAnswer =>
    !!c && typeof c === 'object' && typeof (c as { notification_id?: unknown }).notification_id === 'string')
}

/** One plain sentence for how the ward was told, for the step's notice. */
export function deliveryWords(answers: CriticalDeliveryAnswer[]): string | null {
  if (answers.length === 0) return null
  const told = answers.filter(a => a.posted && !isChange(a)).length
  const changed = answers.filter(a => a.posted && isChange(a)).length
  const missed = answers.filter(a => !a.posted).length
  const parts: string[] = []
  if (told > 0) {
    parts.push(told === 1
      ? 'The critical result was posted to the patient\'s case room.'
      : `${told} critical results were posted to the patients' case rooms.`)
  }
  if (changed > 0) {
    parts.push((changed === 1
      ? 'The change to a critical result reported earlier was posted to the patient\'s case room.'
      : `${changed} changes to critical results reported earlier were posted to the patients' case rooms.`)
      + ' Phone the ward now to tell them the value has changed.')
  }
  if (missed > 0) {
    const closed = answers.some(a => !a.posted && a.reason === 'case_room_closed')
    parts.push((missed === 1
      ? 'A critical result could not be posted'
      : `${missed} critical results could not be posted`)
      + (closed ? ' (the patient has been discharged)' : '')
      + '. Phone the ward now.')
  }
  parts.push('Log the call on Critical results.')
  return parts.join(' ')
}
