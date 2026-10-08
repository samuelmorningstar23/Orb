/**
 * The consulting doctor's allergy review (opd-10a, ruling OPD-S7).
 *
 * An empty allergy list cannot say whether the patient has no known allergies
 * or was never asked. This dialog records which:
 *
 *   * "They have allergies": what the patient names is charted on the patient
 *     (the list the order and bedside interlocks screen). Naming nothing
 *     confirms what is already charted.
 *   * "No known allergies": nothing is charted. Not offered while an allergy
 *     is on the chart: a review cannot say "none" over a charted allergy.
 *   * "Could not be obtained": nothing is charted, and the reason is required.
 *
 * The note can then be signed. The review never changes how the interlocks
 * screen, and typing "nkda" as an allergen charts nothing.
 */
import { useCallback, useState } from 'react'
import { createPortal } from 'react-dom'

import {
  recordAllergyReview,
  type AllergyAssessmentStatus,
  type AllergyReviewResult,
} from '../../../api/opdAllergyReview'
import { useFocusTrap } from '../../../hooks/useFocusTrap'
import './AllergyReviewDialog.css'

interface AllergyReviewDialogProps {
  visitId: string
  patientName: string
  /** The allergies active on the chart now. */
  charted: string[]
  onClose: () => void
  onDone: (result: AllergyReviewResult) => void
}

function errorText(err: unknown): string {
  const detail = (err as { detail?: { message?: unknown } } | null)?.detail
  if (detail && typeof detail.message === 'string') return detail.message
  if (err instanceof Error && err.message) return err.message
  return 'The review was not saved. Try again.'
}

export default function AllergyReviewDialog({
  visitId,
  patientName,
  charted,
  onClose,
  onDone,
}: AllergyReviewDialogProps) {
  const onChart = charted.length > 0
  const [status, setStatus] = useState<AllergyAssessmentStatus | ''>('')
  const [allergens, setAllergens] = useState('')
  const [reason, setReason] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const close = useCallback(() => { if (!busy) onClose() }, [busy, onClose])
  const ref = useFocusTrap<HTMLDivElement>(close)

  const named = allergens.split('\n').map(s => s.trim()).filter(Boolean)
  const ready = !busy && (
    (status === 'allergies_recorded' && (named.length > 0 || onChart))
    || (status === 'none_known' && !onChart)
    || (status === 'unable_to_obtain' && reason.trim().length >= 3)
  )

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!ready) return
    setBusy(true)
    setError('')
    try {
      const result = await recordAllergyReview(visitId, {
        status,
        substances: status === 'allergies_recorded' ? named : undefined,
        reason: status === 'unable_to_obtain' ? reason.trim() : undefined,
        note: note.trim() || undefined,
      })
      onDone(result)
    } catch (err) {
      setError(errorText(err))
      setBusy(false)
    }
  }

  const choice = (value: AllergyAssessmentStatus, label: string, off = false, why = '') => (
    <label className={`opd-ar-choice ${off ? 'opd-ar-choice--off' : ''}`}>
      <input type="radio" name="opd-ar-status" value={value} checked={status === value}
        disabled={busy || off} onChange={() => { setStatus(value); setError('') }} />
      <span>
        {label}
        {why && <span className="opd-ar-choice__why">{why}</span>}
      </span>
    </label>
  )

  return createPortal(
    <div className="opd-modal-backdrop" onClick={close}>
      <div
        className="opd-modal"
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby="opd-ar-title"
        onClick={e => e.stopPropagation()}
      >
        <h2 className="opd-modal__title" id="opd-ar-title">Allergy review for {patientName}</h2>
        <form onSubmit={submit} className="opd-ar-choices">
          <p className="opd-hint">
            {onChart
              ? `On the chart now: ${charted.join(', ')}.`
              : 'No allergy is on the chart. Ask the patient, or whoever is with them.'}
          </p>
          <fieldset className="opd-ar-choices">
            <legend>What did the patient say?</legend>
            {choice('allergies_recorded', 'They have allergies')}
            {choice('none_known', 'No known allergies', onChart,
              onChart ? 'Not available while an allergy is on the chart.' : '')}
            {choice('unable_to_obtain', 'Could not be obtained')}
          </fieldset>

          {status === 'allergies_recorded' && (
            <div className="opd-field">
              <label htmlFor="opd-ar-allergens">
                {onChart ? 'Any other allergies, one per line (leave empty to confirm the chart)'
                  : 'Allergies, one per line'}
              </label>
              <textarea id="opd-ar-allergens" rows={3} value={allergens} disabled={busy}
                placeholder="For example, penicillin" onChange={e => setAllergens(e.target.value)} />
            </div>
          )}
          {status === 'unable_to_obtain' && (
            <div className="opd-field">
              <label htmlFor="opd-ar-reason">Why could they not be obtained?</label>
              <textarea id="opd-ar-reason" rows={2} value={reason} disabled={busy}
                placeholder="For example, the patient cannot speak and nobody with them knows"
                onChange={e => setReason(e.target.value)} />
            </div>
          )}
          <div className="opd-field">
            <label htmlFor="opd-ar-note">Note (optional)</label>
            <input id="opd-ar-note" className="opd-ar-input" type="text" value={note} disabled={busy}
              onChange={e => setNote(e.target.value)} />
          </div>

          {error && <p className="opd-error" role="alert">{error}</p>}
          <div className="opd-modal__actions">
            <button type="button" className="opd-btn opd-btn--quiet" onClick={close} disabled={busy}>
              Cancel
            </button>
            <button type="submit" className="opd-btn opd-btn--primary" disabled={!ready}>
              {busy ? 'Saving…' : 'Save allergy review'}
            </button>
          </div>
        </form>
      </div>
    </div>,
    document.body,
  )
}
