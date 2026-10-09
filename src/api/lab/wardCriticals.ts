/**
 * Orb, the ward's critical laboratory results (lis-16): the card on the nurse
 * and doctor homes, and the ward's read-back acknowledgement.
 *
 * WHAT THE CARD SHOWS is what the laboratory said: its value, its own flag
 * and the limit its pathologist agreed (for a result that came over the
 * interface from another laboratory, that laboratory's flag and the reference
 * range it sent). Orb adds no severity and no score of its own.
 *
 * SCOPE comes from the signed-in account on the server (the patient's ward:
 * the active admission first, else the latest), never from the request. A
 * transferred patient's critical moves to the new ward; a discharged
 * patient's stays on the last ward without a bed. A critical outside this
 * ward is 404, exactly like one that does not exist.
 *
 * WHAT CLOSES ONE from the ward: the name of the person who was told, how
 * they were told and their read-back (ruling LIS-R3, closure ii). The
 * acknowledgement names the `value_told` the card showed; when the value
 * changed after the card loaded the server refuses it 409 'value_changed' and
 * writes nothing, so the card reloads.
 *
 * Behind the laboratory module (on or draining) or the interfaced critical
 * switch; with both off every call answers 404 `module_disabled`. The list
 * read is audited on the server. Timestamps are bare UTC: format them through
 * src/lib/datetime.
 */
import type { FeatureFlagId, HospitalModuleId } from '../../shellConfig'
import { labGet, labPost } from './index'

/** Whether this site serves the critical surfaces (the card asks nothing
 *  otherwise): the laboratory module on or draining (/api/health
 *  modules.lab), or the interfaced critical switch on. */
export function criticalCardOn(
  modules: Readonly<Record<HospitalModuleId, boolean>>,
  features: Readonly<Record<FeatureFlagId, boolean>>,
): boolean {
  return modules.lab === true || features.lab_critical_interfaced === true
}

/** How the ward was told: read on the ward screen, or a phone call from the laboratory. */
export type WardAckMethod = 'in_app' | 'phone'

export interface WardCritical {
  id: string
  status: 'open' | 'acknowledged' | 'closed_unreachable'
  source_lane: 'lis' | 'interfaced'
  /** 'Laboratory', or 'Sending laboratory' for an interfaced result. */
  sender: string
  patient: {
    id: string
    name: string | null
    mrn: string | null
    /** Null for a discharged patient: the bed is someone else's now. */
    bed: string | null
    discharged: boolean
    discharged_at: string | null
  }
  test_name: string
  /** The value first reported. */
  value_display: string
  /** The value the laboratory last told the ward: what an acknowledgement reads back and names. */
  value_told: string
  /** The laboratory withdrew the result (entered in error). */
  withdrawn: boolean
  /** The laboratory's own flag, never one Orb worked out. */
  lab_flag: { code: string; text: string | null } | null
  /** The limit the laboratory's pathologist agreed (laboratory results only). */
  critical_limit_text: string | null
  /** The reference range the sending laboratory sent (interfaced results only). */
  reference_range: string | null
  /** Validated but not yet authorised (ruling LIS-R4). */
  preliminary: boolean
  /** How the result stands, in the server's words. */
  state_text: string | null
  /** What changed after the ward was first told, in the server's words. */
  change_text: string | null
  raised_at: string
  acknowledged: {
    at: string | null
    by_name: string | null
    recipient_name: string | null
    method: string | null
    method_text: string | null
    closure: string | null
  } | null
}

export interface WardCriticalList {
  open: WardCritical[]
  /** The ways a ward can have been told, with their words. */
  methods: Record<WardAckMethod, string>
  /** The roles an acknowledgement may name, with their words. */
  recipient_roles: Record<string, string>
  scope: { department: string | null; hospital_wide: boolean }
}

export function getWardCriticals(): Promise<WardCriticalList> {
  return labGet<WardCriticalList>('/criticals/ward')
}

export interface AcknowledgeRequest {
  /** The person who was told. */
  recipient_name: string
  recipient_role?: string | null
  method: WardAckMethod
  /** Who phoned from the laboratory, when it was a call. */
  caller_name?: string | null
  /** Must be true: the server refuses an acknowledgement without a read-back. */
  read_back: boolean
  note?: string | null
  /** The card's `value_told`. Refused 409 'value_changed' when it no longer stands. */
  value_told: string
}

export function acknowledgeWardCritical(
  notificationId: string, body: AcknowledgeRequest,
): Promise<{ notification: WardCritical }> {
  return labPost<{ notification: WardCritical }>(
    `/criticals/${encodeURIComponent(notificationId)}/acknowledge`, body)
}
