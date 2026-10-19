/**
 * Medication orders API (the order store, medorders-4, W5-E): a thin typed
 * client for /api/medorders. The Orders screen signs CPOE and order-set
 * medicines through signMedOrder (medorders-7, W7-F) and places a CPOE
 * medicine with its fields through createCpoeMedicationOrder. The chart's
 * order list (components/medorders/MedOrderList), its timeline and the
 * doctor's to-sign chip read this module too (medorders-10, W9-D): the
 * status chip words, the supply line, the last dose and which steps a
 * signed-in person is offered all come from the helpers below, so every
 * screen says them the same way.
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
import { apiFetch, socket, type AdministrationRecord, type MedicationSafety } from './client'
import { formatClinicalDateTime } from '../lib/datetime'

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
  /** GET /patient/{id} only (medorders-10): the people on the order by name. */
  prescriber_name?: string | null
  verified_by_name?: string | null
  entered_by_name?: string | null
  status_changed_by_name?: string | null
  /** GET /patient/{id} only: the latest supply row, or null for 'supply not
   *  linked' (no supply was ever requested for this order). */
  supply?: MedOrderSupplyLine | null
  /** GET /patient/{id} only: the last bedside record on the order's ledger. */
  last_dose?: MedOrderLastDose | null
  [key: string]: unknown
}

/** Who did a step on the order's ledger, and when. */
export interface MedOrderActor {
  by: string | null
  by_name: string | null
  by_role: string | null
  at: string | null
}

/** The latest pharmacy supply row behind an order, as the chart lists it. */
export interface MedOrderSupplyLine {
  helix_order_id: string | null
  /** The latest supply event: supply_requested, supply_reviewing,
   *  supply_verified, supply_rejected, dispensed or received. */
  state: string | null
  at: string | null
  requested_at: string | null
  dispensed: MedOrderActor | null
  received: MedOrderActor | null
}

/** The last bedside record on an order's ledger. */
export interface MedOrderLastDose extends MedOrderActor {
  kind: 'administered' | 'not_given'
  reason: string | null
  /** The administration record this dose is: ref_type
   *  'medication_administration' and ref_id its id, as text. The marker for a
   *  dose given across a charted allergy lives on that record
   *  (ledgerAdministration, then givenAcrossAllergy). */
  ref_type?: string | null
  ref_id?: string | null
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

/** Hold, resume and stop (ruling MO-R2 (a): the prescriber only, each with a
 *  reason). The server's routes for these are medorders-6a, which is not
 *  built yet (routes/medorders/lifecycle.py is still empty), so no screen
 *  offers them while this is false. Flip it in the change that serves
 *  POST /medorders/orders/{id}/hold, /resume and /stop. */
export const MEDORDER_HOLD_STOP_ROUTES = false

/** Put a live order on hold. Doses are refused at the bedside while it is
 *  held; 'not given (held)' can always be recorded. */
export function holdMedOrder(orderId: string, reason: string, expectedVersion: number):
Promise<MedOrderStepResult> {
  return apiFetch<MedOrderStepResult>(`${orderPath(orderId)}/hold`, {
    method: 'POST',
    body: JSON.stringify({ reason, expected_version: expectedVersion }),
  })
}

/** Resume a held order. The server screens it again (a 428 carries alerts to
 *  acknowledge, as at sign). */
export function resumeMedOrder(
  orderId: string,
  reason: string,
  expectedVersion: number,
  safetyAck?: { acknowledged: boolean; reason: string },
): Promise<MedOrderStepResult> {
  return apiFetch<MedOrderStepResult>(`${orderPath(orderId)}/resume`, {
    method: 'POST',
    body: JSON.stringify({ reason, expected_version: expectedVersion,
      ...(safetyAck ? { safety_ack: safetyAck } : {}) }),
  })
}

/** Stop a live or held order for good. */
export function stopMedOrder(orderId: string, reason: string, expectedVersion: number):
Promise<MedOrderStepResult> {
  return apiFetch<MedOrderStepResult>(`${orderPath(orderId)}/stop`, {
    method: 'POST',
    body: JSON.stringify({ reason, expected_version: expectedVersion }),
  })
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

// ── The pharmacy's view of a linked order (medorders-5c) ─────────────────────

/** The medication order behind a Helix supply row, as GET /helix/orders and
 *  GET /helix/orders/{id}/timeline add it. Only a supply row linked to an
 *  order carries one (the order store must have been used). */
export interface HelixLinkedOrder {
  id: string
  status: MedOrderStatus
  version: number
  verified_by: string | null
  verified_at: string | null
  verified_by_name?: string | null
  /** The order still needs a pharmacist's own check (an AI-charted drug). */
  needs_verification?: boolean
}

/** One event from the order's ledger as the pharmacy timeline shows it. The
 *  bedside doses (given, not given) are never sent to the pharmacy desk. */
export interface HelixOrderEvent {
  id: number
  kind: string
  from_status: MedOrderStatus | null
  to_status: MedOrderStatus | null
  actor_id: string | null
  actor_role: string | null
  actor_name: string | null
  reason: string | null
  ref_type: string | null
  ref_id: string | null
  created_at: string
}

/** An order's state in words. Status is never shown by colour alone. */
export const MED_ORDER_STATUS_WORDS: Record<MedOrderStatus, string> = {
  draft: 'Not signed yet',
  prescribed: 'Prescribed',
  verified: 'Checked by a pharmacist',
  held: 'On hold',
  stopped: 'Stopped',
  completed: 'Course complete',
  cancelled: 'Cancelled',
  rejected: 'Rejected by pharmacy',
}

/** Each ledger event in the timeline's words. */
export const MED_ORDER_EVENT_WORDS: Record<string, string> = {
  created: 'Order written',
  signed: 'Signed by the prescriber',
  verified: 'Order checked by a pharmacist',
  rejected: 'Order rejected by pharmacy',
  held: 'Put on hold',
  resumed: 'Resumed',
  stopped: 'Stopped',
  completed: 'Course complete',
  cancelled: 'Cancelled',
  amended: 'Order corrected',
  adopted: 'Added to the order record',
  supply_requested: 'Pharmacy asked to supply it',
  supply_reviewing: 'Pharmacy reviewing a supply',
  supply_verified: 'Supply verified',
  supply_rejected: 'Supply rejected',
  dispensed: 'Dispensed',
  received: 'Received on the ward',
  administered: 'Dose given',
  not_given: 'Dose not given',
}

export function medOrderEventWords(kind: string): string {
  return MED_ORDER_EVENT_WORDS[kind] ?? 'Order updated'
}

/** "Order verified by Ravi Kumar at 3 Oct 2026, 10:15 AM", or null when no
 *  pharmacist has checked the order. */
export function orderVerifiedLine(order: HelixLinkedOrder): string | null {
  if (!order.verified_by) return null
  const who = (order.verified_by_name ?? '').trim() || 'a pharmacist'
  const when = formatClinicalDateTime(order.verified_at)
  return when ? `Order verified by ${who} at ${when}` : `Order verified by ${who}`
}

/** What the pharmacist should know about the order behind this supply, in
 *  plain words. `lifecycleOn` is the server's 'med_order_lifecycle' feature:
 *  with it on, verifying or rejecting a supply of an order still waiting for
 *  its check IS that check; with it off the two are separate. */
export function linkedOrderNotice(order: HelixLinkedOrder, lifecycleOn: boolean): string {
  const verified = orderVerifiedLine(order)
  switch (order.status) {
    case 'verified':
      return `${verified ?? 'Order verified'}. Verifying here checks this supply only.`
    case 'prescribed':
      if (!order.needs_verification) return 'Prescribed. No pharmacist check of the order is outstanding.'
      return lifecycleOn
        ? "Waiting for a pharmacist's check of the order. Verifying or rejecting this supply is that check."
        : "Waiting for a pharmacist's check of the order in the Verify Queue. Verifying here checks this supply only."
    case 'held':
      return verified
        ? `On hold. ${verified}.`
        : 'On hold. The prescriber has paused this medicine.'
    case 'draft':
      return 'Not signed yet. Pharmacy cannot supply it until a prescriber signs it.'
    default:
      return `${MED_ORDER_STATUS_WORDS[order.status] ?? 'Ended'}. Pharmacy does not supply an order that has ended.`
  }
}

// ── The chart's words (medorders-10) ─────────────────────────────────────────

/** Who did a step on the ledger, by role ("a nurse"): the chart never prints
 *  a login id. */
const EVENT_ROLE_WORDS: Record<string, string> = {
  doctor: 'a doctor',
  clinician: 'a doctor',
  nurse: 'a nurse',
  pharmacist: 'a pharmacist',
  admin: 'an administrator',
  system: 'Orb',
}

export function medOrderEventActorWords(event: Pick<MedOrderEvent, 'actor_role' | 'actor_id'>): string {
  const role = (event.actor_role ?? '').trim()
  if (role && EVENT_ROLE_WORDS[role]) return EVENT_ROLE_WORDS[role]
  if (event.actor_id === 'system') return 'Orb'
  return role ? `a ${role.replace(/_/g, ' ')}` : 'a colleague'
}

/** Extra words a step carries, from its payload, in plain language. */
export function medOrderEventDetailWords(event: MedOrderEvent): string | null {
  const payload = (event.payload ?? {}) as Record<string, unknown>
  if (event.kind === 'administered' && payload.before_pharmacist_check === true) {
    return 'Given before the pharmacist checked the order'
  }
  if (event.kind === 'amended') {
    const changes = payload.changes
    if (changes && typeof changes === 'object') {
      const fields = Object.keys(changes as Record<string, unknown>)
        .map(f => f.replace(/_/g, ' '))
      if (fields.length) return `Changed: ${fields.join(', ')}`
    }
  }
  return null
}

/** How a status chip is drawn. The label always says the status in words;
 *  the tone only adds a coloured border (never colour alone). */
export type MedOrderTone = 'neutral' | 'ok' | 'warn' | 'danger'

export interface MedOrderChip {
  label: string
  tone: MedOrderTone
}

/** The chart's status chip. `verifyBeforeDose` is the server's
 *  'med_order_verify_before_dose' feature: with it on, every prescribed order
 *  waits for a pharmacist before its first dose (ruling MO-R1). */
export function medOrderChip(
  order: { status: MedOrderStatus; needs_verification?: boolean | null; verified_by?: string | null },
  opts: { verifyBeforeDose?: boolean } = {},
): MedOrderChip {
  switch (order.status) {
    case 'draft':
      return { label: 'Awaiting signature', tone: 'warn' }
    case 'prescribed':
      return order.needs_verification || (opts.verifyBeforeDose && !order.verified_by)
        ? { label: 'Awaiting pharmacist', tone: 'warn' }
        : { label: 'Prescribed', tone: 'neutral' }
    case 'verified':
      return { label: 'Verified by pharmacist', tone: 'ok' }
    case 'held':
      return { label: 'On hold', tone: 'danger' }
    case 'stopped':
      return { label: 'Stopped', tone: 'neutral' }
    case 'completed':
      return { label: 'Completed', tone: 'neutral' }
    case 'cancelled':
      return { label: 'Cancelled', tone: 'neutral' }
    case 'rejected':
      return { label: 'Rejected by pharmacy', tone: 'danger' }
    default:
      return { label: 'Status not known', tone: 'warn' }
  }
}

/** Statuses an order can still move on from. */
export const LIVE_MED_ORDER_STATUSES: readonly MedOrderStatus[] =
  ['draft', 'prescribed', 'verified', 'held']

export function isLiveMedOrder(order: Pick<MedOrder, 'status'>): boolean {
  return LIVE_MED_ORDER_STATUSES.includes(order.status)
}

/** A step a person can take on an order from the chart. */
export type MedOrderStep = 'sign' | 'hold' | 'resume' | 'stop' | 'cancel'

/** Steps that need a written reason (the server's REASON_REQUIRED_KINDS). */
export const REASON_REQUIRED_STEPS: readonly MedOrderStep[] = ['hold', 'resume', 'stop', 'cancel']

export interface MedOrderStepContext {
  /** The signed-in person holds medication.prescribe. */
  mayPrescribe: boolean
  /** The signed-in person's id (a draft's creator may cancel it). */
  userId: string | null
  /** The server's 'med_order_lifecycle' feature: every write needs it. */
  lifecycleOn: boolean
  /** Hold, resume and stop are served (MEDORDER_HOLD_STOP_ROUTES). */
  holdStopAvailable: boolean
}

/** Which steps the chart offers this person on this order, in the order the
 *  buttons are drawn. The server decides; this only avoids offering a press
 *  it is already known to refuse. Ruling MO-R2 (a): sign, hold, resume and
 *  stop are the prescriber's; a draft is cancelled by whoever entered it or a
 *  prescriber; a signed order is never cancelled here (stop it instead). */
export function medOrderSteps(order: Pick<MedOrder, 'status' | 'entered_by'>,
  ctx: MedOrderStepContext): MedOrderStep[] {
  if (!ctx.lifecycleOn) return []
  const steps: MedOrderStep[] = []
  const holdStop = ctx.mayPrescribe && ctx.holdStopAvailable
  switch (order.status) {
    case 'draft':
      if (ctx.mayPrescribe) steps.push('sign')
      if (ctx.mayPrescribe || (ctx.userId !== null && order.entered_by === ctx.userId)) {
        steps.push('cancel')
      }
      break
    case 'prescribed':
    case 'verified':
      if (holdStop) steps.push('hold', 'stop')
      break
    case 'held':
      if (holdStop) steps.push('resume', 'stop')
      break
    default:
      break
  }
  return steps
}

export const MED_ORDER_STEP_LABELS: Record<MedOrderStep, string> = {
  sign: 'Sign',
  hold: 'Hold',
  resume: 'Resume',
  stop: 'Stop',
  cancel: 'Cancel',
}

function actorWords(actor: MedOrderActor | null | undefined): string {
  const name = (actor?.by_name ?? '').trim()
  return name || 'a colleague'
}

function atWords(at: string | null | undefined): string {
  const when = formatClinicalDateTime(at)
  return when ? ` at ${when}` : ''
}

/** The supply line under an order: where the pharmacy's supply of it stands,
 *  or 'Supply not linked' when no supply was ever requested for it (a drug
 *  given from ward stock or the patient's own, or one nobody asked for). */
export function supplyLineWords(order: Pick<MedOrder, 'supply'>): string {
  const supply = order.supply
  if (!supply || !supply.state) return 'Supply not linked'
  switch (supply.state) {
    case 'supply_requested':
      return `Requested from pharmacy${atWords(supply.requested_at ?? supply.at)}`
    case 'supply_reviewing':
      return 'Pharmacy is reviewing the supply'
    case 'supply_verified':
      return 'Supply checked by pharmacy, not yet dispensed'
    case 'supply_rejected':
      return 'Pharmacy rejected the supply'
    case 'dispensed':
      return `Dispensed by ${actorWords(supply.dispensed)}${atWords(supply.dispensed?.at)}, ` +
        'not yet received on the ward'
    case 'received':
      return `Dispensed by ${actorWords(supply.dispensed)}, received on the ward by ` +
        `${actorWords(supply.received)}${atWords(supply.received?.at)}`
    default:
      return 'Supply status not known'
  }
}

/** The chart's administration record a ledger dose points at (a last dose or
 *  a 'Dose given' event: ref_type 'medication_administration', ref_id the
 *  record's id), or undefined when it points at none or the chart has not read
 *  that record. Whether the dose was given across a charted allergy is then
 *  givenAcrossAllergy(record) from api/client, the one derivation every
 *  surface uses. */
export function ledgerAdministration(
  records: readonly AdministrationRecord[] | null | undefined,
  ref: { ref_type?: string | null; ref_id?: string | null } | null | undefined,
): AdministrationRecord | undefined {
  if (!records || !ref || ref.ref_type !== 'medication_administration') return undefined
  if (ref.ref_id === null || ref.ref_id === undefined || String(ref.ref_id).trim() === '') {
    return undefined
  }
  const id = String(ref.ref_id).trim()
  return records.find(a => String(a.id) === id)
}

/** The last bedside record, or null when the order's ledger has none. */
export function lastDoseWords(order: Pick<MedOrder, 'last_dose'>): string | null {
  const dose = order.last_dose
  if (!dose) return null
  const who = actorWords(dose)
  if (dose.kind === 'administered') return `Last dose given by ${who}${atWords(dose.at)}`
  const why = (dose.reason ?? '').trim()
  return `Last dose not given${why ? ` (${why})` : ''}, recorded by ${who}${atWords(dose.at)}`
}

/** The first words of the bedside's refusal for a held order
 *  (server/services/medorders/core.py _HELD_WORDS). The eMAR answers it as a
 *  flat 422 with those words, so this is how the Administer screen knows the
 *  refusal is a hold. MedOrderList.test.tsx pins it against the server file. */
export const HELD_REFUSAL_START = 'This medicine is on hold'

/** Is this eMAR refusal the hold? */
export function isHeldRefusal(message: string | null | undefined): boolean {
  return typeof message === 'string' && message.trim().startsWith(HELD_REFUSAL_START)
}

// ── Live updates ─────────────────────────────────────────────────────────────

export const MEDORDERS_UPDATE_EVENT = 'medorders:update'

/** Listen for order changes on the caller's ward. Returns the unsubscribe. */
export function subscribeMedOrderUpdates(handler: (update: MedOrderUpdate) => void): () => void {
  socket.on(MEDORDERS_UPDATE_EVENT, handler)
  return () => { socket.off(MEDORDERS_UPDATE_EVENT, handler) }
}
