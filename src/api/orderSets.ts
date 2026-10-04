/** Order sets / protocols — the client half of `server/routes/orders_routes.py`.
 *
 *  The backend has been complete and safety-enforced for a while (list, get,
 *  preview, apply, applications, an `order_set_applications` table, audit hooks
 *  and a cited protocol file); `grep -rni "order.set" src/` returned nothing.
 *  A whole demo step — "apply an order set, see the safety gates fire" — had no
 *  surface at all. This module and components/orders/OrderSetsPanel.tsx are it.
 *
 *  THE CONTRACT THAT MATTERS: a set is applied WHOLE OR NOT AT ALL. If any item
 *  is stopped by the allergy interlock or the dose guard, `apply` writes nothing
 *  and returns 428/422 with the per-item verdicts. The UI must never present a
 *  partial success, and must never offer a way to skip the gates.
 */
import { apiFetch } from './client'

export interface OrderSetGuideline {
  name?: string
  citation?: string
  reviewed_by?: string
  reviewed_on?: string
}

export interface OrderSetSummary {
  id: string
  name: string
  indication: string
  guideline: OrderSetGuideline
  item_count: number
  fingerprint: string
}

/** One alert from the medication-safety screen. Non-blocking ones still carry
 *  clinical weight — a moderate cross-reactivity is a caution the prescriber
 *  must SEE, even though it does not stop the set. */
export interface MedicationSafetyAlert {
  type: string
  substance?: string
  reaction?: string
  severity?: string
  recorded_severity?: string
  message: string
  match?: string
  matched_on?: string
}

export interface MedicationSafety {
  drug: string
  allergy_alerts: MedicationSafetyAlert[]
  interaction_alerts: MedicationSafetyAlert[]
  max_severity: string | null
  blocking: boolean
  safe: boolean
  interaction_screened: boolean
  coverage_note: string | null
}

/** One item after the gates have run. `blocked` is the whole point. */
export interface OrderSetVerdict {
  index: number
  order_type: string
  description: string
  code: string | null
  code_system: string | null
  priority: string
  drug_name: string | null
  dose: string | null
  route: string | null
  frequency: string | null
  note: string | null
  edited_fields: string[]
  medication_safety: MedicationSafety | null
  dose_check: unknown | null
  blocked: boolean
  block_kind: string | null
  block_reason: string | null
}

export interface OrderSetPreview {
  patient_id: string
  set: { id: string; name: string; guideline: OrderSetGuideline; fingerprint: string }
  items: OrderSetVerdict[]
  blocked: OrderSetVerdict[]
  would_apply: boolean
  unknown_codes: { code: string; code_system: string; description?: string }[]
}

export interface OrderSetApplication {
  id?: string | number
  set_id: string
  set_name?: string
  applied_at?: string
  applied_by?: string
  order_count?: number
  [k: string]: unknown
}

/** One item of a set as the protocol file defines it. The medication fields
 *  are null on a non-medication item. */
export interface OrderSetItem {
  index: number
  order_type: string
  description: string
  code: string | null
  code_system: string | null
  priority: string
  note: string | null
  drug_name: string | null
  dose: string | null
  route: string | null
  frequency: string | null
}

/** One set in full, as loaded and validated from the protocol file. */
export interface OrderSetDefinition {
  id: string
  name: string
  indication: string | null
  guideline: OrderSetGuideline & { url?: string | null }
  items: OrderSetItem[]
  item_count: number
  fingerprint: string
}

/** A row of the `orders` table, as a set's apply created it (a DRAFT order).
 *  `detail_json` is the stored JSON TEXT naming the set and, for a drug, the
 *  dose and the safety screen. */
export interface AppliedOrder {
  id: string
  patient_id: string
  order_type: 'lab' | 'imaging' | 'medication' | 'procedure' | 'nursing' | 'other'
  code: string | null
  code_system: string | null
  description: string
  priority: 'stat' | 'urgent' | 'routine'
  status: string
  ordered_by: string | null
  source: string
  detail_json: string | null
  result_text: string | null
  result_at: string | null
  created_at: string
  updated_at: string
  order_set_application_id?: number | null
  opd_visit_id?: string | null
}

export interface OrderSetSelection {
  index: number
  include?: boolean
  description?: string
  priority?: string
  dose?: string
  route?: string
  frequency?: string
  safety_ack?: { acknowledged: boolean; reason: string }
}

export async function listOrderSets(): Promise<{
  sets: OrderSetSummary[]
  config: { path: string; config_version: string; fingerprint: string; editable_by: string }
}> {
  return apiFetch('/orders/sets')
}

/** One set in full, with the codes this deployment's terminology table cannot
 *  resolve. */
export async function getOrderSet(setId: string): Promise<{
  set: OrderSetDefinition
  unknown_codes: { code: string; code_system: string }[]
}> {
  return apiFetch(`/orders/sets/${encodeURIComponent(setId)}`)
}

export async function previewOrderSet(
  setId: string,
  patientId: string,
  items?: OrderSetSelection[],
): Promise<OrderSetPreview> {
  return apiFetch(`/orders/sets/${encodeURIComponent(setId)}/preview`, {
    method: 'POST',
    body: JSON.stringify(items ? { patient_id: patientId, items } : { patient_id: patientId }),
  })
}

export async function applyOrderSet(
  setId: string,
  patientId: string,
  items?: OrderSetSelection[],
): Promise<{
  status: string
  application_id: number
  patient_id: string
  set: { id: string; name: string; guideline: OrderSetGuideline; fingerprint: string }
  orders: AppliedOrder[]
  items: OrderSetVerdict[]
}> {
  return apiFetch(`/orders/sets/${encodeURIComponent(setId)}/apply`, {
    method: 'POST',
    body: JSON.stringify(items ? { patient_id: patientId, items } : { patient_id: patientId }),
  })
}

export async function listOrderSetApplications(
  patientId: string,
): Promise<{ applications: OrderSetApplication[] }> {
  return apiFetch(`/orders/sets/applications/${encodeURIComponent(patientId)}`)
}
