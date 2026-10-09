/**
 * The allergy review beside the consultation's Sign button (opd-11; ruling
 * OPD-S7, opd-10a).
 *
 * A note is signed only after an allergy review: on this visit (whatever it
 * said) or a still valid earlier one ("allergies recorded" or "no known
 * allergies"). This panel says which review lets the note be signed, or that
 * none does yet and how to record one. It shows the review as the
 * visit-addressed answer carries it (what it said and when), never an
 * allergen: the allergy list is on the record view, behind the OPD scope.
 *
 * The review itself is recorded in AllergyReviewDialog; this panel only
 * offers it. A review never changes how the allergy interlocks screen.
 */
import type { AllergyReviewState } from '../../../api/opdAllergyReview'
import { formatClinicalDateTime } from '../../../lib/datetime'

interface AllergyReviewPanelProps {
  review: AllergyReviewState | null
  /** Open the review dialog; left out where the review cannot be recorded. */
  onRecord?: () => void
}

export default function AllergyReviewPanel({ review, onRecord }: AllergyReviewPanelProps) {
  const canSign = !!review?.can_sign
  return (
    <div className="opd-cons__review" role="group" aria-label="Allergy review for signing">
      {canSign && review?.assessment ? (
        <p className="opd-hint">
          {review.basis === 'this_visit'
            ? 'Allergy review recorded on this visit.'
            : `Allergy review from an earlier visit, ${formatClinicalDateTime(review.assessment.assessed_at)}.`}
        </p>
      ) : canSign ? (
        <p className="opd-hint">The allergy review lets this note be signed.</p>
      ) : (
        <>
          <p className="opd-hint">
            A note can be signed once the patient&rsquo;s allergies are recorded in an allergy
            review: their allergies, no known allergies, or why they could not be asked. Until then
            the visit can be finished with a reason.
          </p>
          {onRecord && (
            <div className="opd-row__actions">
              <button type="button" className="opd-btn" onClick={onRecord}>
                Record the review now
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
