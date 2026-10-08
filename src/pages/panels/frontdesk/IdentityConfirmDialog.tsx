/**
 * Confirm who the patient is before a visit is attached to their record
 * (opd-6a, ruling OPD-S1).
 *
 * The desk asks the patient and types what they STATE: their name, their
 * date of birth or age, and the last 4 digits of their phone or their UHID.
 * The server compares and answers "match" or "no match", and this dialog
 * shows exactly that and nothing else. It never receives, and so can never
 * show, the stored date of birth or phone: the date of birth is the second
 * factor of the discharge link, and a hospital-wide searcher must not learn
 * it. The name is not filled in from the record either, so the check is what
 * the patient says, not what the screen says.
 *
 * A match hands the stated identifiers to the caller, which sends them again
 * with the visit: the server re-checks there and refuses a visit without
 * them. Five checks that do not match for one patient in an hour lock the
 * check for that hour (429).
 */
import { useCallback, useState } from 'react'
import { createPortal } from 'react-dom'

import {
  checkPatientIdentity,
  errorCode,
  type IndexCard,
  type StatedIdentity,
} from '../../../api/registration'
import { useFocusTrap } from '../../../hooks/useFocusTrap'
import './RegisterPatientView.css'

interface IdentityConfirmDialogProps {
  /** The record the desk chose: only its id, name, UHID and inpatient flag
   *  are used, to say which record is being checked. */
  patient: Pick<IndexCard, 'patient_id' | 'name' | 'uhid' | 'inpatient_now'>
  onConfirmed: (stated: StatedIdentity) => void
  onClose: () => void
}

type AgeMode = 'dob' | 'age'
type SecondMode = 'phone' | 'uhid'
type Verdict = 'match' | 'no_match' | null

function errorText(err: unknown): string {
  if (errorCode(err) === 'identity_check_locked') {
    return 'Too many checks for this patient did not match in the last hour. Ask a colleague '
      + 'to confirm who the patient is.'
  }
  if (err instanceof Error && err.message) return err.message
  return 'The check did not run. Try again.'
}

export default function IdentityConfirmDialog({
  patient,
  onConfirmed,
  onClose,
}: IdentityConfirmDialogProps) {
  const [name, setName] = useState('')
  const [ageMode, setAgeMode] = useState<AgeMode>('dob')
  const [dob, setDob] = useState('')
  const [age, setAge] = useState('')
  const [secondMode, setSecondMode] = useState<SecondMode>('phone')
  const [last4, setLast4] = useState('')
  const [uhid, setUhid] = useState('')
  const [busy, setBusy] = useState(false)
  const [verdict, setVerdict] = useState<Verdict>(null)
  const [error, setError] = useState('')
  const close = useCallback(() => { if (!busy) onClose() }, [busy, onClose])
  const ref = useFocusTrap<HTMLDivElement>(close)

  const ageNumber = age.trim() === '' ? NaN : Number(age)
  const ageOk = ageMode === 'dob'
    ? dob !== ''
    : Number.isInteger(ageNumber) && ageNumber >= 0 && ageNumber <= 130
  const secondOk = secondMode === 'phone' ? /^\d{4}$/.test(last4.trim()) : uhid.trim() !== ''
  const ready = !busy && name.trim() !== '' && ageOk && secondOk

  const stated = (): StatedIdentity => ({
    name: name.trim(),
    ...(ageMode === 'dob' ? { date_of_birth: dob } : { age_years: ageNumber }),
    ...(secondMode === 'phone' ? { phone_last4: last4.trim() } : { uhid: uhid.trim() }),
  })

  /* Any change to what was typed clears the last answer: a "match" must
     always be about the words now on screen. */
  const edit = (fn: () => void) => {
    fn()
    setVerdict(null)
    setError('')
  }

  const check = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!ready) return
    setBusy(true)
    setError('')
    setVerdict(null)
    try {
      const answer = await checkPatientIdentity(patient.patient_id, stated())
      setVerdict(answer.result === 'match' ? 'match' : 'no_match')
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return createPortal(
    <div className="rpv-modal-backdrop" onClick={close}>
      <div
        className="rpv-modal"
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby="rpv-identity-title"
        onClick={e => e.stopPropagation()}
      >
        <h2 className="rpv-modal__title" id="rpv-identity-title">Confirm who the patient is</h2>
        <p className="rpv-lede">
          Checking against {patient.name}
          {patient.uhid ? `, UHID ${patient.uhid}` : ''}. Ask the patient, and type what they
          say. The answer is only match or no match.
        </p>
        {patient.inpatient_now && (
          <p className="rpv-warn">This patient is an inpatient now.</p>
        )}

        <form onSubmit={check} className="rpv">
          <div className="rpv-field">
            <label htmlFor="rpv-id-name">Their name, as they say it</label>
            <input id="rpv-id-name" type="text" value={name} autoComplete="off" disabled={busy}
              onChange={e => edit(() => setName(e.target.value))} />
          </div>

          <fieldset className="rpv-field">
            <legend>Their date of birth or age</legend>
            <div className="rpv-choice">
              <label>
                <input type="radio" name="rpv-id-age" checked={ageMode === 'dob'} disabled={busy}
                  onChange={() => edit(() => setAgeMode('dob'))} />
                Date of birth
              </label>
              <label>
                <input type="radio" name="rpv-id-age" checked={ageMode === 'age'} disabled={busy}
                  onChange={() => edit(() => setAgeMode('age'))} />
                Age in years
              </label>
            </div>
            {ageMode === 'dob' ? (
              <input aria-label="Date of birth they state" type="date" value={dob} disabled={busy}
                onChange={e => edit(() => setDob(e.target.value))} />
            ) : (
              <input aria-label="Age they state, in years" type="number" min={0} max={130}
                inputMode="numeric" value={age} disabled={busy}
                onChange={e => edit(() => setAge(e.target.value))} />
            )}
          </fieldset>

          <fieldset className="rpv-field">
            <legend>The last 4 digits of their phone, or their UHID</legend>
            <div className="rpv-choice">
              <label>
                <input type="radio" name="rpv-id-second" checked={secondMode === 'phone'}
                  disabled={busy} onChange={() => edit(() => setSecondMode('phone'))} />
                Phone, last 4 digits
              </label>
              <label>
                <input type="radio" name="rpv-id-second" checked={secondMode === 'uhid'}
                  disabled={busy} onChange={() => edit(() => setSecondMode('uhid'))} />
                UHID
              </label>
            </div>
            {secondMode === 'phone' ? (
              <input aria-label="Last 4 digits of their phone" type="text" inputMode="numeric"
                maxLength={4} value={last4} disabled={busy} autoComplete="off"
                onChange={e => edit(() => setLast4(e.target.value.replace(/\D/g, '')))} />
            ) : (
              <input aria-label="UHID they state" type="text" value={uhid} disabled={busy}
                autoComplete="off" onChange={e => edit(() => setUhid(e.target.value))} />
            )}
          </fieldset>

          {verdict === 'match' && (
            <p className="rpv-ok" role="status">Match. This is the patient.</p>
          )}
          {verdict === 'no_match' && (
            <p className="rpv-error" role="alert">
              No match. What the patient said does not match this record. Check you chose the
              right patient, and ask again.
            </p>
          )}
          {error && <p className="rpv-error" role="alert">{error}</p>}

          <div className="rpv-actions">
            <button type="button" className="rpv-btn rpv-btn--quiet" onClick={close} disabled={busy}>
              Cancel
            </button>
            {verdict === 'match' ? (
              <button type="button" className="rpv-btn rpv-btn--primary"
                onClick={() => onConfirmed(stated())}>
                Continue
              </button>
            ) : (
              <button type="submit" className="rpv-btn rpv-btn--primary" disabled={!ready}>
                {busy ? 'Checking…' : 'Check'}
              </button>
            )}
          </div>
        </form>
      </div>
    </div>,
    document.body,
  )
}
