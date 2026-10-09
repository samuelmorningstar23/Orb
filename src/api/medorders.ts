/**
 * Medication orders API (the order store, medorders-4, W5-E): a thin typed
 * client for /api/medorders. The Orders screen signs CPOE and order-set
 * medicines through signMedOrder (medorders-7, W7-F) and places a CPOE
 * medicine with its fields through createCpoeMedicationOrder; the chart's
 * order list, the to-sign view and the round's hold groups arrive in later
 * waves.
 *
 * Every call exists only while the server's 'med_order_store' feature is on
 * (ORB_MED_ORDER_STORE). With it off the server answers 404
 * {error: 'feature_disabled'}; the writes (create, sign, cancel) also need
 * 'med_order_lifecycle'. `isFeatureOff(err)` recognises either answer so a
 * screen can say the feature is switched off instead of showing an error.
 *
 * THE DEPARTMENT IS NEVER A PARAMETER. The server derives it from the
 * signed-in account; an order on another ward answers 404, exactly as an
 * order that does not exist.
 *
 * THE SOCKET EVENT CARRIES IDS ONLY. 'medorders:update' names the patient,
 * the order, its status and its version, never a drug: a screen that cares
 * re-reads the order over REST.
 */
import { apiFetch, socket, type MedicationSafety } from './client'

// ── Shapes ───────────────────────────────────────────────────────────────────

export type MedOrderStatus =
  | 'draft' | 'prescribed' | 'verified' | 'held'
  | 'stopped' | 'completed' | 'cancelled' | 'rejected'

export type MedOrderPriority = 'routine' | 'urgent' | 'stat'

export interface MedOrder {
  id: string
  patient_id: string
  admission_id: string | null
  encounter_type: 'ip' | 'op' | 'discharge'
  status: MedOrderStatus
  held_from: MedOrderStatus | null
  version: number
  source: string
  needs_verification: boolean
  drug_name: string
  formulary_id: string | null
  dose: string | null
  route: string | null
  frequency: string | null
  is_prn: boolean
  prn_indication: string | null
  priority: MedOrderPriority | null
  indication: string | null
  notes: string | null
  entered_by: string
  entered_by_role: string | null
  prescriber_id: string | null
  prescribed_at: string | null
  verified_by: string | null
  verified_at: string | null
  status_reason: string | null
  status_changed_by: string | null
  status_changed_at: string | null
  last_safety: Record<string, unknown> | null
  legacy_medication_id: number | null
  created_at: string
  updated_at: string
  /** FHIR MedicationRequest.status for this order. */
  fhir_status: string | null
  [key: string]: unknown
}

/** A chart row the store has not adopted yet, shown as the order it would
 *  become. It has no order id and cannot be signed or cancelled here. */
export interface LegacyMedication {
  legacy: true
  medication_id: number
  drug_name: string
  dose: string | null
  route: string | null
  frequency: string | null
  chart_status: string | null
  status: MedOrderStatus | null
  is_prn: boolean
  needs_verification: boolean
  verified_by: string | null
  verified_at: string | null
  started_at: string | null
  adoptable: boolean
}

export interface PatientMedOrders {
  patient_id: string
  orders: MedOrder[]
  legacy: LegacyMedication[]
}

export interface MedOrderEvent {
  id: number
  med_order_id: string
  patient_id: string
  kind: string
  from_status: MedOrderStatus | null
  to_status: MedOrderStatus | null
  actor_id: string | null
  actor_role: string | null
  entry_point: string | null
  reason: string | null
  ref_type: string | null
  ref_id: string | null
  payload: Record<string, unknown> | null
  created_at: string
}

/** A live order or chart row for the same product, returned as a notice at
 *  create and sign (whether it needs a reason is a later ruling, MO-R6). */
export interface SameIngredientNotice {
  med_order_id: string | null
  medication_id: number | null
  drug_name: string
  status: string
}

export interface MedOrderStepResult {
  order: MedOrder
  /** The ward row written for a live order; null for a draft. */
  medication_id: number | null
  safety: Record<string, unknown> | null
  duplicates: SameIngredientNotice[]
  replayed: boolean
}

export interface ToSignOrder extends MedOrder {
  patient_name: string | null
  patient_ward: string | null
  patient_bed: string | null
  entered_by_name: string | null
  /** Only on drafts: waiting longer than draft_wait_hours (ruling MO-R10). */
  waiting_over_24h?: boolean
}

export interface ToSignQueue {
  drafts: ToSignOrder[]
  entered_by_a_nurse: ToSignOrder[]
  draft_wait_hours: number
  counts: { drafts: number; waiting_over_24h: number; entered_by_a_nurse: number }
}

export interface NewMedOrder {
  patient_id: string
  drug_name: string
  /** 'draft' reaches nothing until a prescriber signs it; 'prescribed' is
   *  created and signed in one step and needs medication.prescribe. A draft
   *  may leave out the dose, route and frequency; signing needs all three
   *  (422 dose_route_and_frequency_required). */
  status?: 'draft' | 'prescribed'
  dose?: string | null
  route?: string | null
  frequency?: string | null
  is_prn?: boolean
  prn_indication?: string | null
  priority?: MedOrderPriority | null
  indication?: string | null
  notes?: string | null
  duration_text?: string | null
  safety_ack?: { acknowledged: boolean; reason: string } | null
}

/** What GET /api/orders/patient/{id} adds to a CPOE or order-set medicine
 *  the order store manages (medorders-7): the linked order's state. */
export interface LinkedMedOrder {
  id: string
  status: MedOrderStatus
  version: number
  source: string
  drug_name: string
  dose: string | null
  route: string | null
  frequency: string | null
  is_prn: boolean
  prn_indication: string | null
  priority: MedOrderPriority | null
  entered_by: string
  entered_by_role: string | null
  entered_by_name: string | null
  prescriber_id: string | null
  prescriber_name: string | null
  prescribed_at: string | null
  verified_by: string | null
  verified_at: string | null
  status_reason: string | null
  status_changed_at: string | null
  legacy_medication_id: number | null
  needs_verification: boolean
  created_at: string
  /** A draft waiting longer than a day for a signature (ruling MO-R10 (a)). */
  waiting_over_24h: boolean
  /** Ruling MO-R11 (a): false says the pharmacy does not supply it; null when
   *  the formulary could not be read. */
  on_formulary: boolean | null
}

/** A CPOE medicine as its fields (the order form with the lifecycle on). */
export interface CpoeMedicationOrder {
  patient_id: string
  /** Must name the drug: the server refuses a description that does not. */
  description: string
  priority: MedOrderPriority
  drug_name: string
  dose: string
  route: string
  frequency: string
  is_prn: boolean
  prn_indication?: string | null
  code?: string | null
  code_system?: string | null
}

export interface MedOrderUpdate {
  patient_id: string
  med_order_id: string
  status: MedOrderStatus
  version: number
}

export interface AdoptionResult {
  adopted: number
  unadoptable: number
  conflict: number
  batches: number
  by_status: Record<string, number>
  unadoptable_medication_ids: number[]
  conflict_medication_ids: number[]
}

// ── Errors ───────────────────────────────────────────────────────────────────

/** True when the server said the order store (or its lifecycle) is off. */
export function isFeatureOff(err: unknown): boolean {
  const e = err as { status?: number; detail?: { error?: string } } | null
  return !!e && e.status === 404 && e.detail?.error === 'feature_disabled'
}

/** The structured refusal code: stale_version, screen_stale, reason_required,
 *  cancel_not_offered, prescriber_required, ... apiFetch keeps the body as
 *  `detail`, except on a 403, where it keeps it as `permissions`. */
export function errorCode(err: unknown): string | null {
  const e = err as { detail?: { error?: unknown }; permissions?: { error?: unknown } } | null
  if (typeof e?.detail?.error === 'string') return e.detail.error
  return typeof e?.permissions?.error === 'string' ? e.permissions.error : null
}

// ── Calls ────────────────────────────────────────────────────────────────────

const orderPath = (orderId: string) => `/medorders/orders/${encodeURIComponent(orderId)}`

export function fetchPatientMedOrders(patientId: string): Promise<PatientMedOrders> {
  return apiFetch<PatientMedOrders>(`/medorders/patient/${encodeURIComponent(patientId)}`)
}

export async function fetchMedOrder(orderId: string): Promise<MedOrder> {
  const body = await apiFetch<{ order: MedOrder }>(orderPath(orderId))
  return body.order
}

export async function fetchMedOrderEvents(orderId: string): Promise<MedOrderEvent[]> {
  const body = await apiFetch<{ med_order_id: string; events: MedOrderEvent[] }>(
    `${orderPath(orderId)}/events`)
  return body.events
}

/** Create a draft, or create and sign. Pass the same `idempotencyKey` on a
 *  retry so the server returns the first order instead of a second one. */
export function createMedOrder(order: NewMedOrder, idempotencyKey?: string): Promise<MedOrderStepResult> {
  const options: RequestInit = { method: 'POST', body: JSON.stringify(order) }
  if (idempotencyKey) options.headers = { 'Idempotency-Key': idempotencyKey }
  return apiFetch<MedOrderStepResult>('/medorders/orders', options)
}

/** The prescriber's signature. `expectedVersion` is the version on screen
 *  (409 stale_version when it moved); a 428 carries alerts to acknowledge. */
export function signMedOrder(
  orderId: string,
  expectedVersion: number,
  safetyAck?: { acknowledged: boolean; reason: string },
): Promise<MedOrderStepResult> {
  return apiFetch<MedOrderStepResult>(`${orderPath(orderId)}/sign`, {
    method: 'POST',
    body: JSON.stringify({ expected_version: expectedVersion, ...(safetyAck ? { safety_ack: safetyAck } : {}) }),
  })
}

/** Cancel an unsigned order, with a reason. */
export async function cancelMedOrderDraft(
  orderId: string,
  reason: string,
  expectedVersion?: number,
): Promise<MedOrder> {
  const body = await apiFetch<{ order: MedOrder }>(`${orderPath(orderId)}/cancel`, {
    method: 'POST',
    body: JSON.stringify({ reason, ...(expectedVersion != null ? { expected_version: expectedVersion } : {}) }),
  })
  return body.order
}

/** Place a CPOE medicine with the lifecycle on: POST /api/orders with the
 *  drug's fields in detail.medication. The server writes the orders draft and
 *  its linked draft medication order together; nothing reaches the ward until
 *  a prescriber signs it. A 428 throws the typed safety error (apiFetch), to
 *  re-submit with the acknowledgement. */
export function createCpoeMedicationOrder(
  order: CpoeMedicationOrder,
  safetyAck?: { acknowledged: boolean; reason: string },
): Promise<{ status: string; order: Record<string, unknown>; safety?: MedicationSafety }> {
  const body: Record<string, unknown> = {
    patient_id: order.patient_id,
    order_type: 'medication',
    description: order.description,
    priority: order.priority,
    code: order.code ?? null,
    code_system: order.code_system ?? null,
    detail: {
      medication: {
        drug_name: order.drug_name,
        dose: order.dose,
        route: order.route,
        frequency: order.frequency,
        is_prn: order.is_prn,
        prn_indication: order.is_prn ? order.prn_indication ?? null : null,
      },
    },
  }
  if (safetyAck) body.safety_ack = safetyAck
  return apiFetch('/orders', { method: 'POST', body: JSON.stringify(body) })
}

export function fetchToSignQueue(): Promise<ToSignQueue> {
  return apiFetch<ToSignQueue>('/medorders/queue/to-sign')
}

export function fetchMedOrderParity(): Promise<Record<string, unknown>> {
  return apiFetch<Record<string, unknown>>('/medorders/admin/parity')
}

export function adoptMedOrders(patientIds?: string[]): Promise<AdoptionResult> {
  return apiFetch<AdoptionResult>('/medorders/admin/adopt', {
    method: 'POST',
    body: JSON.stringify(patientIds ? { patient_ids: patientIds } : {}),
  })
}

// ── Live updates ─────────────────────────────────────────────────────────────

export const MEDORDERS_UPDATE_EVENT = 'medorders:update'

/** Listen for order changes on the caller's ward. Returns the unsubscribe. */
export function subscribeMedOrderUpdates(handler: (update: MedOrderUpdate) => void): () => void {
  socket.on(MEDORDERS_UPDATE_EVENT, handler)
  return () => { socket.off(MEDORDERS_UPDATE_EVENT, handler) }
}
