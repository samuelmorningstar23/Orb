/**
 * Leave and absconded (adt-10, over POST /api/adt/admissions/{id}/leave,
 * /leave-end, /absconded and /returned).
 *
 * Records that a patient has gone on leave, is back from leave, has gone
 * without leave (or did not come back from leave), or is back on the ward
 * after absconding. The bed stays the patient's throughout.
 *
 * WHAT IS OFFERED follows where the patient is now: on the ward, a leave or
 * absconding; on leave, back from leave or absconding; absconded, back on the
 * ward. The server refuses anything else and its words are printed here.
 *
 * A REASON is required for a leave (who allowed it, where the patient goes)
 * and for absconding (when and how the patient was found missing, and who
 * was told). The time starts at the moment the screen opened and may be moved
 * back inside the site's back-time window (ADT_BACKTIME_HOURS, read from
 * GET /api/adt/meta, 72 hours when it cannot be read).
 *
 * DISPLAY ONLY. The boards then say 'Not on ward since'. Observation and
 * medication due times and escalation are NOT paused (adt-11): this screen
 * says so, so nobody takes a quiet board for a paused one.
 *
 * Doctors and the administrator only (ruling ADT-S7 held); the server
 * refuses everyone else. One Idempotency-Key per screen, sent on every
 * attempt, so a lost reply records the change once. The container draws the
 * surface and labels it with `titleId`.
 */
import { useEffect, useId, useState } from 'react'

import { getAdtMeta } from '../../api/adt'
import { DEFAULT_BACKTIME_HOURS, fromLocalInput, newWriteKey, toLocalInput } from '../../api/adt/endStay'
import {
  CHANGE_WORDS,
  PRESENCE_WORDS,
  changesFrom,
  needsReason,
  presenceFailureWords,
  recordPresence,
  sinceWords,
  type PresenceChange,
  type PresenceResult,
  type PresenceStatus,
} from '../../api/adt/presence'
import { formatClinicalDateTime } from '../../lib/datetime'
import { assertOnlineForWrite, isOfflineWriteRefused } from '../../lib/offline/writeGuard'
import './EndStay.css'

export interface PresencePatient {
  name: string | null
  mrn: string | null
  ward?: string | null
  bed?: string | null
}

interface PresenceDrawerProps {
  admissionId: string
  patient: PresencePatient
  /** Where the patient is now, as the board said: null is on the ward. */
  presence: PresenceStatus | null
  /** When the patient was last on the ward, when they are not on it. */
  presenceSince?: string | null
  titleId?: string
  /** Something was recorded and the person has read what happened. */
  onDone: (result: PresenceResult) => void
  /** Closed before anything was recorded. */
  onClose: () => void
}

/** How far ahead of this computer's clock a time may sit (the server's slack). */
const FUTURE_SLACK_MS = 5 * 60 * 1000

function place(ward?: string | null, bed?: string | null): string {
  const parts = [ward, bed ? `bed ${bed}` : null].filter(Boolean)
  return parts.length ? parts.join(', ') : 'Not recorded'
}

function timeProblem(when: Date | null, backtimeHours: number, now: Date = new Date()): string | null {
  if (!when) return 'Enter the date and time.'
  if (when.getTime() > now.getTime() + FUTURE_SLACK_MS) return 'That time is in the future.'
  if (now.getTime() - when.getTime() > backtimeHours * 3600 * 1000) {
    return `This can be recorded up to ${backtimeHours} hours back. Older entries need an administrator.`
  }
  return null
}

const REASON_HINT: Readonly<Record<PresenceChange, string>> = {
  leave: 'Who allowed the leave and where the patient is going. It is part of the record.',
  absconded: 'When and how the patient was found missing, and who was told. It is part of the record.',
  'leave-end': 'Optional.',
  returned: 'Optional. How the patient came back, and anything the team should know.',
}

const WHEN_LABEL: Readonly<Record<PresenceChange, string>> = {
  leave: 'When the patient left the ward',
  absconded: 'When the patient was found missing',
  'leave-end': 'When the patient came back',
  returned: 'When the patient came back',
}

export default function PresenceDrawer({
  admissionId, patient, presence, presenceSince = null, titleId, onDone, onClose,
}: PresenceDrawerProps) {
  const madeId = useId()
  const headingId = titleId ?? `presence-${madeId}`
  const fieldId = `presence-field-${madeId}`

  const [key] = useState(newWriteKey)
  const offered = changesFrom(presence)
  const [change, setChange] = useState<PresenceChange | null>(offered.length === 1 ? offered[0] : null)
  const [when, setWhen] = useState(() => toLocalInput(new Date()))
  const [expected, setExpected] = useState('')
  const [reason, setReason] = useState('')
  const [notes, setNotes] = useState('')
  const [backtimeHours, setBacktimeHours] = useState(DEFAULT_BACKTIME_HOURS)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<PresenceResult | null>(null)

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

  const whenDate = fromLocalInput(when)
  const problem = timeProblem(whenDate, backtimeHours)
  const expectedDate = expected ? fromLocalInput(expected) : null
  const expectedProblem = change === 'leave' && expected
    ? (!expectedDate ? 'Enter the date and time, or leave it empty.'
      : whenDate && expectedDate.getTime() <= whenDate.getTime()
        ? 'The expected return must be after the leave starts.' : null)
    : null
  const reasonMissing = !!change && needsReason(change) && !reason.trim()
  const ready = !!change && !problem && !expectedProblem && !reasonMissing && !submitting

  const submit = async () => {
    if (!ready || !change || !whenDate) return
    setSubmitting(true)
    setError(null)
    try {
      assertOnlineForWrite('record this')
      setResult(await recordPresence(admissionId, change, {
        reason, notes, occurred_at: whenDate.toISOString(),
        expected_return_at: change === 'leave' && expectedDate ? expectedDate.toISOString() : undefined,
      }, key))
    } catch (err) {
      setError(isOfflineWriteRefused(err) ? err.message : presenceFailureWords(err))
    } finally {
      setSubmitting(false)
    }
  }

  if (result) {
    const now = result.presence_status
    return (
      <section className="es" aria-labelledby={headingId} data-testid="presence-result">
        <h2 id={headingId} className="es__title">
          {now ? PRESENCE_WORDS[now] : 'Back on the ward'}
        </h2>
        <div className="es__box es__box--ok" role="status">
          <p className="es__box-text">
            {result.replayed
              ? 'This had already been recorded. Nothing new was written.'
              : `Recorded for ${formatClinicalDateTime(result.movement.moved_at ?? '')}.`}
          </p>
          <p className="es__box-text">The bed stays this patient&apos;s.</p>
        </div>
        {now && (
          <p className="es__note">
            The boards now say the patient is not on the ward. Observation and medication due
            times and escalations are not paused for a patient off the ward.
          </p>
        )}
        <div className="es__actions">
          <button type="button" className="es__btn es__btn--primary" onClick={() => onDone(result)}>
            Done
          </button>
        </div>
      </section>
    )
  }

  const nowDate = new Date()
  return (
    <section className="es" aria-labelledby={headingId} data-testid="presence">
      <h2 id={headingId} className="es__title">Leave or absconded</h2>
      <p className="es__lead">
        Records when this patient leaves the ward or comes back. The bed stays theirs.
      </p>

      <dl className="es__readback" aria-label="The patient">
        <div className="es__readback-item">
          <dt className="es__term">Name</dt>
          <dd className="es__value">{patient.name || 'Not recorded'}</dd>
        </div>
        <div className="es__readback-item">
          <dt className="es__term">MRN</dt>
          <dd className="es__value">{patient.mrn || 'Not on file'}</dd>
        </div>
        <div className="es__readback-item">
          <dt className="es__term">Ward and bed</dt>
          <dd className="es__value">{place(patient.ward, patient.bed)}</dd>
        </div>
        <div className="es__readback-item">
          <dt className="es__term">Now</dt>
          <dd className="es__value" data-testid="presence-now">
            {presence
              ? `${PRESENCE_WORDS[presence]}${presenceSince ? `, not on the ward since ${sinceWords(presenceSince)}` : ''}`
              : 'On the ward'}
          </dd>
        </div>
      </dl>

      <fieldset className="es__fieldset">
        <legend className="es__legend">What to record</legend>
        <div className="es__choices">
          {offered.map((c) => (
            <label key={c} className={`es__choice${change === c ? ' es__choice--on' : ''}`}>
              <input
                type="radio"
                className="es__choice-input"
                name={`${fieldId}-change`}
                value={c}
                checked={change === c}
                onChange={() => setChange(c)}
                disabled={submitting}
              />
              {c === 'absconded' && presence === 'on_leave' ? 'Did not come back from leave (absconded)' : CHANGE_WORDS[c]}
            </label>
          ))}
        </div>
      </fieldset>

      {change && (
        <>
          <div className="es__field">
            <label className="es__label" htmlFor={`${fieldId}-when`}>{WHEN_LABEL[change]}</label>
            <input
              id={`${fieldId}-when`}
              className="es__input"
              type="datetime-local"
              value={when}
              min={toLocalInput(new Date(nowDate.getTime() - backtimeHours * 3600 * 1000))}
              max={toLocalInput(nowDate)}
              onChange={(e) => setWhen(e.target.value)}
              disabled={submitting}
              aria-invalid={!!problem}
              aria-describedby={`${fieldId}-when-state`}
            />
            <span id={`${fieldId}-when-state`} className={problem ? 'es__field-error' : 'es__hint'}>
              {problem ?? `Up to ${backtimeHours} hours back.`}
            </span>
          </div>

          {change === 'leave' && (
            <div className="es__field">
              <label className="es__label" htmlFor={`${fieldId}-expected`}>Expected back (optional)</label>
              <input
                id={`${fieldId}-expected`}
                className="es__input"
                type="datetime-local"
                value={expected}
                onChange={(e) => setExpected(e.target.value)}
                disabled={submitting}
                aria-invalid={!!expectedProblem}
                aria-describedby={`${fieldId}-expected-state`}
              />
              <span id={`${fieldId}-expected-state`} className={expectedProblem ? 'es__field-error' : 'es__hint'}>
                {expectedProblem ?? 'Recorded with the leave.'}
              </span>
            </div>
          )}

          <div className="es__field">
            <label className="es__label" htmlFor={`${fieldId}-reason`}>
              {needsReason(change) ? 'Reason' : 'Note'}
            </label>
            <textarea
              id={`${fieldId}-reason`}
              className="es__textarea"
              value={reason}
              maxLength={2000}
              onChange={(e) => setReason(e.target.value)}
              disabled={submitting}
              aria-required={needsReason(change) ? 'true' : undefined}
            />
            <span className="es__hint">{REASON_HINT[change]}</span>
          </div>

          <div className="es__field">
            <label className="es__label" htmlFor={`${fieldId}-notes`}>Other notes (optional)</label>
            <textarea
              id={`${fieldId}-notes`}
              className="es__textarea"
              value={notes}
              maxLength={2000}
              onChange={(e) => setNotes(e.target.value)}
              disabled={submitting}
            />
          </div>

          <p className="es__note">
            Observation and medication due times and escalations are not paused for a patient off
            the ward. The boards say where the patient is.
          </p>
        </>
      )}

      {error && (
        <div className="es__box es__box--error" role="alert">
          <p className="es__error-text">{error}</p>
        </div>
      )}

      <div className="es__actions">
        <button type="button" className="es__btn" onClick={onClose} disabled={submitting}>
          Cancel
        </button>
        <button
          type="button"
          className="es__btn es__btn--primary"
          onClick={() => { void submit() }}
          disabled={!ready}
        >
          {submitting ? 'Recording' : change ? `Record: ${CHANGE_WORDS[change].toLowerCase()}` : 'Record'}
        </button>
      </div>
    </section>
  )
}
