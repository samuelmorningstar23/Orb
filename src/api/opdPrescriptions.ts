/**
 * Orb, the outpatient prescription (opd-12):
 *   GET  /api/opd/visits/{visit_id}/prescriptions
 *   POST /api/opd/visits/{visit_id}/prescriptions
 *   POST /api/opd/visits/{visit_id}/prescriptions/{order_id}/cancel
 *   GET  /api/opd/visits/{visit_id}/prescriptions/print
 *
 * A line is a medication order in the shared store, written for an
 * outpatient: it never reaches the ward chart or the eMAR, and outpatient
 * dispensing is the pharmacy's own lane. Every call needs the 'opd' feature
 * AND the medication-order store (ORB_MED_ORDER_STORE); on an Orb Ward Record
 * computer the drug chart stays on paper and every call answers 404.
 * `prescriptionsOff(err)` recognises all three so the screen shows nothing.
 *
 * Writing a line is screened on the server: the allergy review first (ruling
 * OPD-S7: 409 allergy_review_required until one exists), then the drug-safety
 * gate (allergy, interaction, dose) and the patient's other outpatient
 * prescriptions. A severe alert answers 428, which apiFetch throws as a
 * SafetyAckRequiredError (isSafetyAckRequired); re-send with a SafetyAck
 * written by a prescriber.
 *
 * Visit-addressed answers never name an allergen: the print carries the
 * allergy review as status, label and time only.
 */
import { apiFetch, type MedicationSafety, type SafetyAck } from './client'
import type { AllergyReviewSummary } from './opdAllergyReview'

export interface OpdPrescription {
  id: string
  opd_visit_id: string | null
  drug_name: string
  dose: string | null
  route: string | null
  frequency: string | null
  is_prn: boolean
  prn_indication: string | null
  duration_days: number | null
  quantity: number | null
  instructions: string | null
  indication: string | null
  status: string
  status_label: string
  /** True while the line stands (prescribed). */
  live: boolean
  version: number
  /** A UTC instant: render through formatClinicalDateTime. */
  prescribed_at: string | null
  prescriber_name: string | null
  /** When the course ends (start plus the duration), a UTC instant, or null. */
  course_ends_at: string | null
  cancel_reason: string | null
  ended_at: string | null
  ended_by_name: string | null
  /** A severe alert or a dose block was overridden with a reason. */
  safety_overridden: boolean
}

export interface PrescriptionInput {
  drug_name: string
  dose: string
  route: string
  frequency: string
  is_prn?: boolean
  prn_indication?: string
  duration_days?: number
  quantity?: number
  instructions?: string
  indication?: string
}

export interface SameIngredientNotice {
  med_order_id: string | null
  medication_id: number | null
  drug_name: string
  status: string
}

export interface PrescriptionWriteResult {
  prescription: OpdPrescription
  /** The screen the line passed: sub-severe alerts and coverage notes to show.
   *  Null on a replayed request. */
  safety: (MedicationSafety & { override?: { by: string; by_name: string | null; reason: string } | null }) | null
  /** The same medicine already prescribed or charted (a notice only). */
  duplicates: SameIngredientNotice[]
  replayed: boolean
}

export interface PrescriptionCancelResult {
  prescription: OpdPrescription
  /** A print that listed this line was already made: take that copy back. */
  printed_before: boolean
}

export interface PrescriptionPrint {
  kind: 'prescription'
  printed_at: string
  visit: { id: string; visit_date: string | null; department: string | null; token_number: number | null }
  patient: { name: string | null; uhid: string | null; age: number | null; sex: string | null }
  /** What the allergy review said and when; never an allergen. */
  allergy_review: AllergyReviewSummary | null
  /** The signed note's diagnoses; empty while the note is a draft. */
  diagnoses: string[]
  prescriptions: Pick<OpdPrescription, 'drug_name' | 'dose' | 'route' | 'frequency' | 'is_prn'
    | 'prn_indication' | 'duration_days' | 'quantity' | 'instructions' | 'prescribed_at'
    | 'prescriber_name'>[]
  doctors: { name: string | null; registration_number: string | null }[]
}

const linesPath = (visitId: string) => `/opd/visits/${encodeURIComponent(visitId)}/prescriptions`

/** True when the server has no prescriptions here: the 'opd' feature or the
 *  medication-order store is off, or the drug chart is not part of this
 *  computer (Orb Ward Record). The screen then shows no prescription at all. */
export function prescriptionsOff(err: unknown): boolean {
  const e = err as { status?: number; detail?: unknown } | null
  if (!e || e.status !== 404) return false
  // services/drug_chart.DRUG_CHART_OFF, the Ward Record profile's answer.
  if (typeof e.detail === 'string') return e.detail.toLowerCase().includes('drug chart')
  const code = (e.detail as { error?: unknown } | null)?.error
  return code === 'feature_disabled'
}

/** A fresh request key for one line. The server keeps keys per prescriber, so
 *  a lost reply re-sent with the same key answers the line it wrote. */
export function newLineKey(): string {
  const c = globalThis.crypto as Crypto | undefined
  if (c && typeof c.randomUUID === 'function') return c.randomUUID()
  return `rx-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

export function fetchVisitPrescriptions(visitId: string): Promise<{ visit_id: string; patient_id: string; prescriptions: OpdPrescription[] }> {
  return apiFetch(linesPath(visitId))
}

export function writePrescription(
  visitId: string,
  input: PrescriptionInput,
  key: string,
  safetyAck?: SafetyAck,
): Promise<PrescriptionWriteResult> {
  return apiFetch<PrescriptionWriteResult>(linesPath(visitId), {
    method: 'POST',
    headers: { 'Idempotency-Key': key },
    body: JSON.stringify(safetyAck ? { ...input, safety_ack: safetyAck } : input),
  })
}

export function cancelPrescription(
  visitId: string,
  orderId: string,
  reason: string,
  expectedVersion?: number,
): Promise<PrescriptionCancelResult> {
  return apiFetch<PrescriptionCancelResult>(`${linesPath(visitId)}/${encodeURIComponent(orderId)}/cancel`, {
    method: 'POST',
    body: JSON.stringify(expectedVersion != null ? { reason, expected_version: expectedVersion } : { reason }),
  })
}

export function fetchPrescriptionPrint(visitId: string): Promise<PrescriptionPrint> {
  return apiFetch<PrescriptionPrint>(`${linesPath(visitId)}/print`)
}

/** One line in plain words: "500 mg, Oral, TDS, for 5 days, quantity 15". */
export function prescriptionLine(p: Pick<OpdPrescription, 'dose' | 'route' | 'frequency' | 'is_prn'
  | 'prn_indication' | 'duration_days' | 'quantity'>): string {
  return [
    p.dose,
    p.route,
    p.is_prn
      ? `when needed${p.prn_indication ? ` for ${p.prn_indication}` : ''}${p.frequency ? ` (${p.frequency})` : ''}`
      : p.frequency,
    p.duration_days ? `for ${p.duration_days} ${p.duration_days === 1 ? 'day' : 'days'}` : null,
    p.quantity ? `quantity ${p.quantity}` : null,
  ].filter(Boolean).join(', ')
}
