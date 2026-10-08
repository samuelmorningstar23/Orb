/**
 * Orb, the OPD allergy review (opd-10a, ruling OPD-S7):
 * POST /api/opd/visits/{visit_id}/allergy-review.
 *
 * An empty allergy list means "no known allergies" OR "never asked", and the
 * allergy interlocks cannot tell them apart. The consulting doctor's review
 * says which: the allergies the patient has (charted on the patient, which is
 * what the interlocks screen), "no known allergies", or why they could not be
 * asked. A note is signed only after a review on this visit or a still valid
 * earlier one. Only opd.consult (doctor, clinician) may record one.
 *
 * The record view (GET /api/opd/patients/{id}/record) carries the latest
 * review in full as `allergy_assessment`, and the consultation read
 * (GET /api/opd/visits/{id}/consultation) carries `allergy_review` with
 * `basis` and `assessment`; both are read here with the helpers below so
 * src/api/opd.ts keeps its types.
 *
 * Only the record view, behind the OPD scope arm, names allergens. A visit id
 * of any date reaches the visit-addressed answers (the consultation read, the
 * signing refusal, the review's own answer), so they carry a review as an
 * AllergyReviewSummary: what it said and when, nothing it names. A review is
 * refused 409 'visit_not_current' on a visit outside the OPD scope window.
 */
import { apiFetch } from './client'

export type AllergyAssessmentStatus = 'none_known' | 'allergies_recorded' | 'unable_to_obtain'

export interface AllergyAssessment {
  id: number
  status: AllergyAssessmentStatus
  status_label: string
  /** The active allergies confirmed by an 'allergies_recorded' review. */
  substances: string[]
  /** Why the allergies could not be obtained ('unable_to_obtain'). */
  reason: string | null
  note: string | null
  opd_visit_id: string | null
  department: string | null
  assessed_by: string
  assessed_by_name: string | null
  /** A UTC instant: render through formatClinicalDateTime. */
  assessed_at: string
}

/** A review as a visit-addressed answer carries it: no allergen, note,
 *  reason, clinic or clinician. The full review is on the record view. */
export interface AllergyReviewSummary {
  status: AllergyAssessmentStatus
  status_label: string
  /** A UTC instant: render through formatClinicalDateTime. */
  assessed_at: string
}

export interface AllergyReviewState {
  status: AllergyAssessmentStatus | 'not_reviewed' | 'recorded'
  can_sign: boolean
  charted_count: number
  reviewed_this_visit?: boolean
  /** 'this_visit', 'earlier_review', or null when the note cannot be signed. */
  basis?: 'this_visit' | 'earlier_review' | null
  assessment?: AllergyReviewSummary | null
}

export interface AllergyReviewInput {
  status: AllergyAssessmentStatus
  /** Allergens as typed; the server splits on ';', ',' and '|' and never
   *  charts "nkda", "nil" and the like. */
  substances?: string[]
  reason?: string
  note?: string
}

export interface AllergyReviewResult {
  visit_id: string
  patient_id: string
  assessment: AllergyReviewSummary
  /** Allergens charted by this review. */
  charted: string[]
  /** Allergens on the chart as inactive, made active again by this review. */
  reactivated: string[]
  allergy_review: AllergyReviewState
}

export function recordAllergyReview(visitId: string, input: AllergyReviewInput): Promise<AllergyReviewResult> {
  const body: Record<string, unknown> = { status: input.status }
  if (input.substances && input.substances.length) body.substances = input.substances
  if (input.reason) body.reason = input.reason
  if (input.note) body.note = input.note
  return apiFetch<AllergyReviewResult>(
    `/opd/visits/${encodeURIComponent(visitId)}/allergy-review`,
    { method: 'POST', body: JSON.stringify(body) },
  )
}

/** The latest review on an OPD record answer, or null when none is recorded
 *  (or the answer predates opd-10a). */
export function assessmentOf(record: unknown): AllergyAssessment | null {
  const value = (record as { allergy_assessment?: unknown } | null)?.allergy_assessment
  return value && typeof value === 'object' ? (value as AllergyAssessment) : null
}

/** The consultation read's review state, with the opd-10a fields when present. */
export function reviewStateOf(view: unknown): AllergyReviewState | null {
  const value = (view as { allergy_review?: unknown } | null)?.allergy_review
  return value && typeof value === 'object' ? (value as AllergyReviewState) : null
}
