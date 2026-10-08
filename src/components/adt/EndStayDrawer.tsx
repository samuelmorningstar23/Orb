/**
 * End of stay (adt-9, over adt-8a's POST /api/adt/admissions/{id}/end).
 *
 * Records how a stay ended when no discharge summary is drafted: left against
 * medical advice (LAMA), discharged against medical advice (DAMA), absconded
 * or death. A routine discharge, a referral out and a transfer out are the
 * chart's Discharge, which drafts the summary.
 *
 * READ-BACK (ruling ADT-S3, strictest option). The patient's name, MRN and
 * ward and bed are read back at the top, and the MRN must be typed from the
 * wristband. The record button stays off until the typed MRN matches the one
 * on file, compared as the server compares it (no spaces, no capital
 * letters). The server checks again; a mismatch is 409 read_back_mismatch,
 * nothing is written, and its words are printed here.
 *
 * WHEN. The time the stay ended starts at the time this screen opened and
 * may be moved back up to the site's back-time window (ADT_BACKTIME_HOURS,
 * read from GET /api/adt/meta, 72 hours when it cannot be read). The server
 * refuses a time in the future, before the admission or beyond the window,
 * and those words are printed too.
 *
 * WHAT IS STILL OPEN (F050). Open and acknowledged deterioration escalations
 * are listed, each with a way to close it with a reason, exactly as the
 * chart's discharge screen does. Recording the end does NOT close them (held
 * ruling R009): this screen says so and never blocks on them. The care
 * bundles still running are listed too; the end cancels those, as every
 * discharge does. A list that could not be read says so; it is never shown
 * as empty.
 *
 * AFTERWARDS it says what the server recorded (the outcome kept for NABH and
 * whether it was flagged for the records desk, the bundles cancelled, the
 * escalations still open, the portal login switched off on a death, that no
 * discharge link was issued) and offers 'Recorded in error', which opens the
 * reopen screen in place.
 *
 * Doctors only (discharge.generate, inside the patient's department): the
 * containers offer it only to a role that holds that, and the server refuses
 * everyone else. The container draws the surface (a chart modal, or inline
 * under the bed board) and labels it with `titleId`.
 */
import { useEffect, useId, useState } from 'react'

import { getAdtMeta } from '../../api/adt'
import {
  bundleWords,
  DEFAULT_BACKTIME_HOURS,
  END_STAY_DRAWER_TYPES,
  END_TYPE_WORDS,
  endStayFailureWords,
  endTimeProblem,
  endTypeWords,
  fromLocalInput,
  mrnMatches,
  needsReason,
  newWriteKey,
  recordEndOfStay,
  toLocalInput,
  type EndStayRequest,
  type EndStayResult,
  type EndType,
  type OpenEscalationRow,
  type ReopenResult,
} from '../../api/adt/endStay'
import { getPatientBundles, type CareBundle } from '../../api/bundles'
import { getOpenEscalations, resolveEscalation } from '../../api/client'
import { formatClinicalDateTime } from '../../lib/datetime'
import { assertOnlineForWrite, isOfflineWriteRefused } from '../../lib/offline/writeGuard'
import { useClinicalSwitches } from '../../shellConfig'
import ReopenStayDialog from './ReopenStayDialog'
import './EndStay.css'

export interface EndStayPatient {
  id: string
  name: string | null
  mrn: string | null
  ward?: string | null
  bed?: string | null
}

/** What happened, handed to the container when the person presses Done. */
export interface EndStayOutcome {
  ended: EndStayResult
  /** Set when the end was then reopened as recorded in error. */
  reopened: ReopenResult | null
}

interface EndStayDrawerProps {
  admissionId: string
  patient: EndStayPatient
  /** The end type chosen before this screen opened (the chart's discharge
   *  screen), if any. Nothing is chosen otherwise. */
  initialEndType?: EndType | null
  /** The id the container labels its dialog with; one is made otherwise. */
  titleId?: string
  /** Something was recorded and the person has read what happened. */
  onDone: (outcome: EndStayOutcome) => void
  /** Closed before anything was recorded. */
  onClose: () => void
}

type Listing<T> = { state: 'loading' } | { state: 'ready'; rows: T[] } | { state: 'failed' }

function place(ward?: string | null, bed?: string | null): string {
  const parts = [ward, bed ? `bed ${bed}` : null].filter(Boolean)
  return parts.length ? parts.join(', ') : 'Not recorded'
}

function dispositionWords(value: string | null | undefined): string {
  if (value === 'death_within_48h') return 'Death within 48 hours of admission'
  return endTypeWords(value)
}

export default function EndStayDrawer({
  admissionId, patient, initialEndType = null, titleId, onDone, onClose,
}: EndStayDrawerProps) {
  const madeId = useId()
  const headingId = titleId ?? `end-stay-${madeId}`
  const fieldId = `end-stay-field-${madeId}`
  const { scoringEnabled } = useClinicalSwitches()

  // One key for every attempt from this screen: a reply lost on the ward
  // network and a second press record the end once.
  const [key] = useState(newWriteKey)
  const [endType, setEndType] = useState<EndType | null>(
    initialEndType && END_STAY_DRAWER_TYPES.includes(initialEndType) ? initialEndType : null)
  const [when, setWhen] = useState(() => toLocalInput(new Date()))
  const [reason, setReason] = useState('')
  const [notes, setNotes] = useState('')
  const [typedMrn, setTypedMrn] = useState('')
  const [backtimeHours, setBacktimeHours] = useState(DEFAULT_BACKTIME_HOURS)
  const [escalations, setEscalations] = useState<Listing<OpenEscalationRow>>({ state: 'loading' })
  const [bundles, setBundles] = useState<Listing<CareBundle>>({ state: 'loading' })
  const [closeReasons, setCloseReasons] = useState<Record<string, string>>({})
  const [closing, setClosing] = useState<string | null>(null)
  const [closeError, setCloseError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<EndStayResult | null>(null)
  const [reopening, setReopening] = useState(false)

  // The site's back-time window. Advisory: the server enforces its own.
  useEffect(() => {
    let live = true
    Promise.resolve()
      .then(() => getAdtMeta())
      .then((meta) => {
        const hours = Number(meta?.settings?.backtime_hours)
        if (live && Number.isFinite(hours) && hours > 0) setBacktimeHours(hours)
      })
      .catch(() => { /* keep the default; the server checks the window anyway */ })
    return () => { live = false }
  }, [])

  // What is still open on the patient. Inside the promise chain so any
  // failure, even a synchronous one, lands in the catch and is SAID.
  useEffect(() => {
    let live = true
    Promise.resolve()
      .then(() => getOpenEscalations(patient.id))
      .then((rows) => {
        if (live) setEscalations({ state: 'ready', rows: Array.isArray(rows) ? rows : [] })
      })
      .catch(() => { if (live) setEscalations({ state: 'failed' }) })
    Promise.resolve()
      .then(() => getPatientBundles(patient.id, true))
      .then((rows) => {
        if (live) {
          setBundles({
            state: 'ready',
            rows: (Array.isArray(rows) ? rows : []).filter((b) => b.status === 'active'),
          })
        }
      })
      .catch(() => { if (live) setBundles({ state: 'failed' }) })
    return () => { live = false }
  }, [patient.id])

  const mrnOnFile = !!(patient.mrn && patient.mrn.trim())
  const matches = mrnMatches(typedMrn, patient.mrn)
  const whenDate = fromLocalInput(when)
  const timeProblem = endTimeProblem(whenDate, { backtimeHours })
  const reasonNeeded = needsReason(endType)
  const ready = !!endType && matches && !timeProblem && (!reasonNeeded || reason.trim().length > 0)
    && !submitting

  const closeEscalation = async (id: string) => {
    const why = (closeReasons[id] || '').trim()
    if (why.length < 3 || closing) return
    setClosing(id)
    setCloseError(null)
    try {
      await resolveEscalation(id, why, endType === 'death' ? 'patient_died' : 'patient_discharged')
      setEscalations((prev) => (prev.state === 'ready'
        ? { state: 'ready', rows: prev.rows.filter((e) => e.id !== id) }
        : prev))
      setCloseReasons((prev) => {
        const next = { ...prev }
        delete next[id]
        return next
      })
    } catch (err) {
      setCloseError(`The escalation was not closed: ${(err instanceof Error && err.message) || 'the server did not say why'}.`)
    } finally {
      setClosing(null)
    }
  }

  const submit = async () => {
    if (!ready || !endType || !whenDate) return
    setSubmitting(true)
    setError(null)
    const request: EndStayRequest = {
      end_type: endType,
      confirm_mrn: typedMrn.trim(),
      occurred_at: whenDate.toISOString(),
    }
    if (reason.trim()) request.reason = reason.trim()
    if (notes.trim()) request.notes = notes.trim()
    try {
      assertOnlineForWrite('record the end of this stay')
      setResult(await recordEndOfStay(admissionId, request, key))
    } catch (err) {
      setError(isOfflineWriteRefused(err) ? err.message : endStayFailureWords(err))
    } finally {
      setSubmitting(false)
    }
  }

  // ── Recorded in error: the reopen screen, in place ──────────────────────
  if (result && reopening) {
    return (
      <ReopenStayDialog
        admissionId={admissionId}
        patient={patient}
        endType={result.end_type}
        endedAt={result.discharged_at}
        titleId={headingId}
        onClose={() => setReopening(false)}
        onDone={(reopened) => onDone({ ended: result, reopened })}
      />
    )
  }

  // ── What was recorded ───────────────────────────────────────────────────
  if (result) {
    const stillOpen = result.open_escalations ?? []
    const cancelled = result.bundles_cancelled ?? []
    const disposition = result.disposition ?? null
    return (
      <section className="es" aria-labelledby={headingId} data-testid="end-stay-result">
        <h2 id={headingId} className="es__title">
          {result.end_type === 'death' ? 'Death recorded' : 'Stay ended'}
        </h2>
        <div className="es__box es__box--ok" role="status">
          <p className="es__box-text">
            {result.replayed
              ? 'This end of stay had already been recorded. Nothing new was written.'
              : `${endTypeWords(result.end_type)}, recorded for ${formatClinicalDateTime(result.discharged_at)}.`}
          </p>
          <p className="es__box-text">No discharge summary was drafted and no discharge link was issued.</p>
          {result.bed_freed ? <p className="es__box-text">The bed is free for the next patient.</p> : null}
        </div>

        {disposition?.needs_reconciliation && (
          <div className="es__box es__box--warn">
            <p className="es__box-text">
              The records desk had already recorded this stay&apos;s outcome as
              {' '}{dispositionWords(disposition.disposition)}. That entry is kept and flagged for
              them to reconcile with {dispositionWords(disposition.end_of_stay_disposition).toLowerCase()}.
            </p>
          </div>
        )}

        {result.portal_login_deactivated && (
          <p className="es__note">
            The patient&apos;s portal login is switched off. Reopening the stay switches it back on.
          </p>
        )}

        {cancelled.length > 0 && (
          <div className="es__box">
            <p className="es__box-title">Care bundles cancelled</p>
            <ul className="es__list">
              {cancelled.map((b) => <li key={b.id} className="es__list-item">{bundleWords(b)}</li>)}
            </ul>
          </div>
        )}

        {stillOpen.length > 0 && (
          <div className="es__box es__box--warn" data-testid="end-stay-still-open">
            <p className="es__box-title">
              {stillOpen.length === 1
                ? '1 deterioration escalation is still open'
                : `${stillOpen.length} deterioration escalations are still open`}
            </p>
            <p className="es__box-text">
              Recording the end does not close them, and they keep escalating until somebody
              closes each one with a reason.
            </p>
          </div>
        )}

        <div className="es__actions">
          <button type="button" className="es__btn" onClick={() => setReopening(true)}>
            Recorded in error
          </button>
          <button
            type="button"
            className="es__btn es__btn--primary"
            onClick={() => onDone({ ended: result, reopened: null })}
          >
            Done
          </button>
        </div>
      </section>
    )
  }

  // ── The form ────────────────────────────────────────────────────────────
  const now = new Date()
  return (
    <section className="es" aria-labelledby={headingId} data-testid="end-stay">
      <h2 id={headingId} className="es__title">End of stay</h2>
      <p className="es__lead">
        Records how this stay ended. No discharge summary is drafted. Check the patient below
        against the wristband before you record anything.
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
      </dl>

      <fieldset className="es__fieldset">
        <legend className="es__legend">How the stay ended</legend>
        <div className="es__choices">
          {END_STAY_DRAWER_TYPES.map((t) => (
            <label key={t} className={`es__choice${endType === t ? ' es__choice--on' : ''}`}>
              <input
                type="radio"
                className="es__choice-input"
                name={`${fieldId}-type`}
                value={t}
                checked={endType === t}
                onChange={() => setEndType(t)}
                disabled={submitting}
              />
              {END_TYPE_WORDS[t]}
            </label>
          ))}
        </div>
        <span className="es__hint">
          A routine discharge, a referral out or a transfer out is recorded from the chart with
          Discharge, which drafts the summary.
        </span>
      </fieldset>

      <div className="es__field">
        <label className="es__label" htmlFor={`${fieldId}-when`}>When the stay ended</label>
        <input
          id={`${fieldId}-when`}
          className="es__input"
          type="datetime-local"
          value={when}
          min={toLocalInput(new Date(now.getTime() - backtimeHours * 3600 * 1000))}
          max={toLocalInput(now)}
          onChange={(e) => setWhen(e.target.value)}
          disabled={submitting}
          aria-invalid={!!timeProblem}
          aria-describedby={`${fieldId}-when-state`}
        />
        <span id={`${fieldId}-when-state`} className={timeProblem ? 'es__field-error' : 'es__hint'}>
          {timeProblem ?? `Up to ${backtimeHours} hours back, and not before the admission.`}
        </span>
      </div>

      {reasonNeeded && (
        <div className="es__field">
          <label className="es__label" htmlFor={`${fieldId}-reason`}>Reason</label>
          <textarea
            id={`${fieldId}-reason`}
            className="es__textarea"
            value={reason}
            maxLength={2000}
            onChange={(e) => setReason(e.target.value)}
            disabled={submitting}
            aria-required="true"
          />
          <span className="es__hint">Required for this end of stay. It is part of the record.</span>
        </div>
      )}

      <div className="es__field">
        <label className="es__label" htmlFor={`${fieldId}-notes`}>Notes (optional)</label>
        <textarea
          id={`${fieldId}-notes`}
          className="es__textarea"
          value={notes}
          maxLength={2000}
          onChange={(e) => setNotes(e.target.value)}
          disabled={submitting}
        />
      </div>

      <div
        className={`es__box${escalations.state === 'ready' && escalations.rows.length === 0 ? '' : ' es__box--warn'}`}
        data-testid="end-stay-escalations"
      >
        {escalations.state === 'loading' && <p className="es__box-text">Checking for open escalations</p>}
        {escalations.state === 'failed' && (
          <p className="es__box-text">
            Could not read the open escalations. Check the patient&apos;s escalations on the chart:
            recording the end does not close them.
          </p>
        )}
        {escalations.state === 'ready' && escalations.rows.length === 0 && (
          <p className="es__box-text">No deterioration escalation is open on this patient.</p>
        )}
        {escalations.state === 'ready' && escalations.rows.length > 0 && (
          <>
            <p className="es__box-title">
              {escalations.rows.length === 1
                ? '1 deterioration escalation is still open'
                : `${escalations.rows.length} deterioration escalations are still open`}
            </p>
            <p className="es__box-text">
              Recording the end does not close them; they keep escalating until somebody closes
              each one with a reason. Closing them here does not hold up the end of stay.
            </p>
            {escalations.rows.map((e) => {
              const why = closeReasons[e.id] || ''
              const canClose = why.trim().length >= 3 && closing === null
              return (
                <div key={e.id} className="es__escalation">
                  <p className="es__escalation-line">
                    Raised {formatClinicalDateTime(e.raised_at)}
                    {scoringEnabled && typeof e.peak_news2 === 'number' ? `, highest NEWS2 ${e.peak_news2}` : ''}
                    {e.status === 'acknowledged' ? ', acknowledged' : ', not acknowledged'}
                  </p>
                  <label className="es__hint" htmlFor={`${fieldId}-esc-${e.id}`}>Reason for closing</label>
                  <div className="es__escalation-row">
                    <input
                      id={`${fieldId}-esc-${e.id}`}
                      className="es__input"
                      value={why}
                      maxLength={300}
                      onChange={(ev) => setCloseReasons((prev) => ({ ...prev, [e.id]: ev.target.value }))}
                      disabled={submitting}
                    />
                    <button
                      type="button"
                      className="es__btn es__btn--small"
                      onClick={() => closeEscalation(e.id)}
                      disabled={!canClose || submitting}
                    >
                      {closing === e.id ? 'Closing' : 'Close escalation'}
                    </button>
                  </div>
                </div>
              )
            })}
            {closeError && <p className="es__error-text" role="alert">{closeError}</p>}
          </>
        )}
      </div>

      <div className="es__box" data-testid="end-stay-bundles">
        {bundles.state === 'loading' && <p className="es__box-text">Checking for running care bundles</p>}
        {bundles.state === 'failed' && (
          <p className="es__box-text">
            Could not read the care bundles. Any still running are cancelled when the stay ends.
          </p>
        )}
        {bundles.state === 'ready' && bundles.rows.length === 0 && (
          <p className="es__box-text">No care bundle is running.</p>
        )}
        {bundles.state === 'ready' && bundles.rows.length > 0 && (
          <>
            <p className="es__box-title">Cancelled when the stay ends</p>
            <ul className="es__list">
              {bundles.rows.map((b) => (
                <li key={b.id} className="es__list-item">
                  {bundleWords(b)}, started {formatClinicalDateTime(b.triggered_at)}
                </li>
              ))}
            </ul>
          </>
        )}
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
        <span
          id={`${fieldId}-mrn-state`}
          className={matches ? 'es__field-ok' : (typedMrn.trim() ? 'es__field-error' : 'es__hint')}
        >
          {!mrnOnFile
            ? 'No MRN is on file for this patient, so the end of stay cannot be confirmed here. Ask the records desk to add it.'
            : matches
              ? 'Matches the MRN on file.'
              : typedMrn.trim()
                ? 'This does not match the MRN on file.'
                : 'The end is recorded only when this matches the MRN on file.'}
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
          {submitting ? 'Recording' : endType === 'death' ? 'Record death' : 'Record end of stay'}
        </button>
      </div>
    </section>
  )
}
