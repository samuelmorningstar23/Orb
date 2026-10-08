/**
 * Reopen a stay whose end was recorded in error (adt-9, over adt-8b's
 * POST /api/adt/admissions/{id}/reopen).
 *
 * WHEN. A death recorded against the wrong patient, a LAMA keyed on the wrong
 * bed: an end with NO discharge summary. A discharge with a summary is
 * corrected by a new admission, as it always was; the server refuses it with
 * words (summary_on_file) and this screen prints them.
 *
 * WHAT IT ASKS. A typed reason (part of the record) and the MRN typed back
 * from the wristband. The button stays off until the typed MRN matches the
 * one on file, compared as the server compares it; the server checks again
 * and a mismatch (409 read_back_mismatch) writes nothing.
 *
 * WHAT IT SAYS AFTERWARDS. Whether the bed was taken back or the patient now
 * waits for one (and why, in the server's words), whether a portal login was
 * switched back on, and which care bundles the end cancelled and the reopen
 * did NOT restart.
 *
 * Doctors only (discharge.generate, inside the ended stay's department). A
 * container offers it only to a role that holds that; the server refuses
 * everyone else.
 *
 * The container draws the surface (a chart modal, or inline under the bed
 * board) and labels it with `titleId`.
 */
import { useId, useState } from 'react'

import {
  bundleWords,
  endStayFailureWords,
  endTypeWords,
  mrnMatches,
  newWriteKey,
  reopenStay,
  type ReopenResult,
} from '../../api/adt/endStay'
import { formatClinicalDateTime } from '../../lib/datetime'
import { assertOnlineForWrite, isOfflineWriteRefused } from '../../lib/offline/writeGuard'
import './EndStay.css'

export interface ReopenStayPatient {
  name: string | null
  mrn: string | null
  ward?: string | null
  bed?: string | null
}

interface ReopenStayDialogProps {
  admissionId: string
  patient: ReopenStayPatient
  /** How the stay was recorded as ending, when the container knows. */
  endType?: string | null
  /** When it was recorded as ending (UTC), when the container knows. */
  endedAt?: string | null
  /** The id the container labels its dialog with; one is made otherwise. */
  titleId?: string
  /** The stay was reopened and the person has read what happened. */
  onDone: (result: ReopenResult) => void
  /** Closed without reopening anything. */
  onClose: () => void
}

function place(ward?: string | null, bed?: string | null): string {
  const parts = [ward, bed ? `bed ${bed}` : null].filter(Boolean)
  return parts.length ? parts.join(', ') : 'Not recorded'
}

export default function ReopenStayDialog({
  admissionId, patient, endType, endedAt, titleId, onDone, onClose,
}: ReopenStayDialogProps) {
  const madeId = useId()
  const headingId = titleId ?? `reopen-${madeId}`
  const fieldId = `reopen-field-${madeId}`
  // One key for every attempt from this screen: a reply lost on the ward
  // network and a second press reopen the stay once.
  const [key] = useState(newWriteKey)
  const [reason, setReason] = useState('')
  const [typedMrn, setTypedMrn] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<ReopenResult | null>(null)

  const mrnOnFile = !!(patient.mrn && patient.mrn.trim())
  const matches = mrnMatches(typedMrn, patient.mrn)
  const ready = matches && reason.trim().length > 0 && !submitting

  const submit = async () => {
    if (!ready) return
    setSubmitting(true)
    setError(null)
    try {
      assertOnlineForWrite('reopen this stay')
      const answer = await reopenStay(admissionId, { reason: reason.trim(), confirm_mrn: typedMrn.trim() }, key)
      setResult(answer)
    } catch (err) {
      setError(isOfflineWriteRefused(err) ? err.message : endStayFailureWords(err, 'reopen'))
    } finally {
      setSubmitting(false)
    }
  }

  if (result) {
    const bundles = result.bundles_not_restored ?? []
    return (
      <section className="es" aria-labelledby={headingId} data-testid="reopen-stay-result">
        <h2 id={headingId} className="es__title">Stay reopened</h2>
        <div className="es__box es__box--ok" role="status">
          <p className="es__box-text">
            {result.replayed
              ? 'This reopen had already been recorded. Nothing new was written.'
              : `${patient.name || 'The patient'} is an inpatient again. The end of this stay is kept in the record, marked as recorded in error.`}
          </p>
          {result.corrected_end_type !== undefined && (
            <p className="es__box-text">
              Corrected: {endTypeWords(result.corrected_end_type)}
              {result.corrected_discharged_at ? `, recorded for ${formatClinicalDateTime(result.corrected_discharged_at)}` : ''}.
            </p>
          )}
        </div>
        {result.bed_retaken === true && (
          <p className="es__note">
            Back in {place(result.ward, result.bed)}.
            {result.bed_out_of_service
              ? ' Housekeeping has that bed recorded as out of service; ask them to correct it.'
              : ''}
          </p>
        )}
        {result.awaiting_bed === true && (
          <div className="es__box es__box--warn">
            <p className="es__box-text">
              {result.bed_not_retaken_words ? `${result.bed_not_retaken_words}. ` : ''}
              The patient is waiting for a bed. Assign one on the bed board.
            </p>
          </div>
        )}
        {result.portal_login_restored === true && (
          <p className="es__note">The patient&apos;s portal login is switched back on.</p>
        )}
        {bundles.length > 0 && (
          <div className="es__box es__box--warn">
            <p className="es__box-title">Care bundles not restarted</p>
            <ul className="es__list">
              {bundles.map((b) => <li key={b.id} className="es__list-item">{bundleWords(b)}</li>)}
            </ul>
            <p className="es__box-text">Restart any that still apply from the chart.</p>
          </div>
        )}
        <div className="es__actions">
          <button type="button" className="es__btn es__btn--primary" onClick={() => onDone(result)}>
            Done
          </button>
        </div>
      </section>
    )
  }

  return (
    <section className="es" aria-labelledby={headingId} data-testid="reopen-stay">
      <h2 id={headingId} className="es__title">Reopen a stay recorded in error</h2>
      <p className="es__lead">
        Use this only when the end of this stay was recorded in error, for example a death
        recorded against the wrong patient. A stay with a discharge summary cannot be reopened
        here; it is corrected by a new admission.
      </p>

      <dl className="es__readback" aria-label="The patient">
        <div className="es__readback-item">
          <dt className="es__term">Name</dt>
          <dd className="es__value">{patient.name || 'Not recorded'}</dd>
        </div>
        <div className="es__readback-item">
          <dt className="es__term">MRN</dt>
          <dd className="es__value">{mrnOnFile ? patient.mrn : 'Not on file'}</dd>
        </div>
        <div className="es__readback-item">
          <dt className="es__term">Ward and bed</dt>
          <dd className="es__value">{place(patient.ward, patient.bed)}</dd>
        </div>
        {(endType || endedAt) && (
          <div className="es__readback-item">
            <dt className="es__term">Recorded as</dt>
            <dd className="es__value">
              {endType ? endTypeWords(endType) : 'Stay ended'}
              {endedAt ? `, ${formatClinicalDateTime(endedAt)}` : ''}
            </dd>
          </div>
        )}
      </dl>

      <div className="es__box">
        <p className="es__box-title">What reopening does</p>
        <ul className="es__list">
          <li>The stay is active again and the patient returns to the ward lists.</li>
          <li>The bed is taken back if it is still free; otherwise the patient waits for a bed.</li>
          <li>The NABH outcome for this stay is voided, never deleted, and a correction is added to the movement record.</li>
          <li>A portal login switched off when a death was recorded is switched back on.</li>
          <li>Care bundles cancelled when the stay ended are not restarted.</li>
        </ul>
      </div>

      <div className="es__field">
        <label className="es__label" htmlFor={`${fieldId}-reason`}>Why the end was recorded in error</label>
        <textarea
          id={`${fieldId}-reason`}
          className="es__textarea"
          value={reason}
          maxLength={2000}
          onChange={(e) => setReason(e.target.value)}
          disabled={submitting}
          aria-required="true"
        />
        <span className="es__hint">This is part of the record.</span>
      </div>

      <div className="es__field">
        <label className="es__label" htmlFor={`${fieldId}-mrn`}>Type the MRN from the wristband to confirm</label>
        <input
          id={`${fieldId}-mrn`}
          className="es__input es__input--mrn"
          value={typedMrn}
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => setTypedMrn(e.target.value)}
          disabled={submitting || !mrnOnFile}
          aria-invalid={typedMrn.trim() !== '' && !matches}
          aria-describedby={`${fieldId}-mrn-state`}
        />
        <span id={`${fieldId}-mrn-state`} className={matches ? 'es__field-ok' : (typedMrn.trim() ? 'es__field-error' : 'es__hint')}>
          {!mrnOnFile
            ? 'No MRN is on file for this patient, so the stay cannot be reopened here. Ask the records desk to add it.'
            : matches
              ? 'Matches the MRN on file.'
              : typedMrn.trim()
                ? 'This does not match the MRN on file.'
                : 'The stay reopens only when this matches the MRN on file.'}
        </span>
      </div>

      {error && (
        <div className="es__box es__box--error" role="alert">
          <p className="es__error-text">{error}</p>
        </div>
      )}

      <div className="es__actions">
        <button type="button" className="es__btn" onClick={onClose} disabled={submitting}>Cancel</button>
        <button type="button" className="es__btn es__btn--primary" onClick={submit} disabled={!ready}>
          {submitting ? 'Reopening' : 'Reopen stay'}
        </button>
      </div>
    </section>
  )
}
