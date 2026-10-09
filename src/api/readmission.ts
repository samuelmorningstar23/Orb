/**
 * Readmitting a discharged patient onto their own record (opd-17c).
 *
 *   POST /api/admissions/readmit   the readmission
 *
 * Exists only while the server's 'readmission' feature is on (404
 * feature_disabled otherwise). The admission form learns which records may be
 * readmitted from its own refusal: when a new admission matches a patient the
 * index already holds, the 409 possible_existing_patient answer carries
 * readmission_available and marks each record it lists with readmittable.
 *
 * What the readmission does, by ruling (all checked on the server):
 *   * OPD-S1 (a): who the patient is comes from the record; the form sends
 *     only what the patient STATES, checked there, never filled in from it.
 *   * OPD-S8 (a): every medicine the earlier stay left on the chart is
 *     stopped; the answer lists them for the prescriber to prescribe again.
 *   * ADT-S9 (a): the earlier stay's observations are never shown as current,
 *     and the form sends no observations: the ward charts a fresh set.
 *   * R029 (2): the patient's Bridge login is kept with a fresh one-time
 *     password, shown once.
 */
import { apiFetch } from './client'
import type { AdmissionResult } from './client'
import type { StatedIdentity } from './admitExisting'

/** A record the patient index listed, as its minimal card shows it. */
export interface ReadmitCandidate {
  patient_id: string
  uhid: string | null
  name: string
  sex: string | null
  age: number | null
  last_visit_date: string | null
}

/** A medicine the readmission stopped, for the prescriber to prescribe again. */
export interface PreviousStayMedicine {
  medication_id: number
  drug_name: string
  dose: string | null
  route: string | null
  frequency: string | null
  as_needed: boolean | null
  started_at: string | null
  was_on_hold: boolean
}

/** What a readmission adds to an admission's answer. */
export interface ReadmissionFields {
  readmission?: boolean
  portal_password_reissued?: boolean
  previous_stay_medicines_stopped?: number
  /** False when the sender does not read the chart after the readmission, or
   *  on an Orb Ward Record computer: then the medicines are counted, not named. */
  previous_stay_medicines_listed?: boolean
  previous_stay_medicines?: PreviousStayMedicine[]
}

export type ReadmissionResult = AdmissionResult & ReadmissionFields & {
  replayed?: boolean
  portal_login_created?: boolean
  case_room_reused?: boolean
  location_warning?: string | null
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null
}

/** The records an admission refusal says may be readmitted, or [] when the
 *  refusal is anything else or readmission is switched off. */
export function readmitCandidatesOf(detail: unknown): ReadmitCandidate[] {
  if (!detail || typeof detail !== 'object') return []
  const d = detail as Record<string, unknown>
  if (d.error !== 'possible_existing_patient' || d.readmission_available !== true) return []
  const cards = Array.isArray(d.candidates) ? d.candidates : []
  return cards.flatMap(c => {
    if (!c || typeof c !== 'object') return []
    const card = c as Record<string, unknown>
    const id = text(card.patient_id)
    const name = text(card.name)
    if (card.readmittable !== true || !id || !name) return []
    return [{
      patient_id: id,
      uhid: text(card.uhid),
      name,
      sex: text(card.sex),
      age: typeof card.age === 'number' ? card.age : null,
      last_visit_date: text(card.last_visit_date),
    }]
  })
}

/** What an outpatient-visit preview says when the admission is a
 *  readmission, or null. */
export function previewReadmissionNote(preview: unknown): string | null {
  if (!preview || typeof preview !== 'object') return null
  const p = preview as Record<string, unknown>
  return p.readmission === true ? (text(p.readmission_note) ?? '') : null
}

/** "Amoxicillin 500 mg Oral TDS" or "... when needed". */
export function previousStayMedicineWords(m: PreviousStayMedicine): string {
  return [m.drug_name, m.dose, m.route, m.as_needed ? 'when needed' : m.frequency]
    .filter(Boolean).join(' ')
}

export interface ReadmitRequest {
  patient_id: string
  identity_check: StatedIdentity | null
  [field: string]: unknown
}

export async function readmitPatient(
  body: ReadmitRequest,
  idempotencyKey: string,
): Promise<ReadmissionResult> {
  return apiFetch<ReadmissionResult>('/admissions/readmit', {
    method: 'POST',
    headers: { 'Idempotency-Key': idempotencyKey },
    body: JSON.stringify(body),
  })
}
