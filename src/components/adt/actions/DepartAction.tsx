/**
 * Bed board tile action: Left the ward (adt-17b, ruling ADT-S3, a build
 * default for Samuel to confirm).
 *
 * On an Orb Ward Record computer the ward clerk records that a patient left
 * the ward, with the time. The patient's name, MRN and bed are read back and
 * the MRN must be typed from the wristband: the record button stays off until
 * it matches, and the server checks again (409 read_back_mismatch writes
 * nothing). The server ends the stay through its one end-of-stay path, so the
 * discharge preconditions apply. No end type is stated (the clerk does not
 * say how the stay ended, so none is guessed), no discharge summary is
 * drafted and no patient link is issued.
 *
 * WHO. Offered to the ward clerk only ('role:ward_clerk', which
 * BedBoardPanel's permission check answers from the signed-in role: the
 * server's lane is the ward clerk's alone, whatever else holds adt.bed), and
 * the screen says so plainly anywhere but an Orb Ward Record computer, where
 * a doctor's discharge ends the stay. A patient on leave or
 * absconded is not offered it (a doctor records how that stay ended), and
 * only a patient the clerk may see (actionNeedsOccupant).
 *
 * ONE SCREEN PER STAY: the form is keyed by the admission, so a second bed
 * gets a fresh screen and a new Idempotency-Key.
 */
import { useEffect, useId, useState } from 'react'

import { getAdtMeta } from '../../../api/adt'
import { clerkFailureWords, isClerkRole, recordLeftWard, type LeftWardResult } from '../../../api/adt/clerk'
import {
  DEFAULT_BACKTIME_HOURS,
  endTimeProblem,
  fromLocalInput,
  mrnMatches,
  newWriteKey,
  toLocalInput,
} from '../../../api/adt/endStay'
import { formatClinicalDateTime } from '../../../lib/datetime'
import { assertOnlineForWrite, isOfflineWriteRefused } from '../../../lib/offline/writeGuard'
import { currentRole } from '../../../lib/roles'
import { useClinicalSwitches } from '../../../shellConfig'
import '../EndStay.css'
import type { TileActionProps } from './index'

export const actionId = 'left-ward'
export const actionLabel = 'Left the ward'
export const actionOrder = 85
export const actionPermission = 'role:ward_clerk'
export const actionStatuses = 'occupied,boarder'
export const actionNeedsOccupant = 1

function Notice({ words, onClose }: { words: string; onClose: () => void }) {
  return (
    <div className="es">
      <p className="es__note">{words}</p>
      <div className="es__actions">
        <button type="button" className="es__btn" onClick={onClose}>Close</button>
      </div>
    </div>
  )
}

export default function DepartAction({ tile, ward, onDone, onClose }: TileActionProps) {
  const { wardRecord } = useClinicalSwitches()
  const occupant = tile.occupant
  if (!occupant) {
    return <Notice onClose={onClose}
      words="Nobody you can see is in this bed, so there is no stay to close here." />
  }
  if (!isClerkRole(currentRole())) {
    return <Notice onClose={onClose} words="'Left the ward' is recorded by the ward clerk." />
  }
  if (!wardRecord) {
    return <Notice onClose={onClose}
      words="On this computer a doctor's discharge ends the stay. 'Left the ward' is recorded by the ward clerk only on an Orb Ward Record computer." />
  }
  return (
    <LeftWardForm
      key={occupant.admission_id}
      admissionId={occupant.admission_id}
      name={occupant.name}
      mrn={occupant.mrn}
      place={`${ward.code}, bed ${tile.bed_label ?? tile.unit_code}`}
      onDone={onDone}
      onClose={onClose}
    />
  )
}

interface LeftWardFormProps {
  admissionId: string
  name: string | null
  mrn: string | null
  place: string
  onDone: () => void
  onClose: () => void
}

function LeftWardForm({ admissionId, name, mrn, place, onDone, onClose }: LeftWardFormProps) {
  const madeId = useId()
  const headingId = `left-ward-${madeId}`
  // One key for every attempt from this screen: a lost reply and a second
  // press record the departure once.
  const [key] = useState(newWriteKey)
  const [when, setWhen] = useState(() => toLocalInput(new Date()))
  const [notes, setNotes] = useState('')
  const [typedMrn, setTypedMrn] = useState('')
  const [backtimeHours, setBacktimeHours] = useState(DEFAULT_BACKTIME_HOURS)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<LeftWardResult | null>(null)

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

  const mrnOnFile = !!(mrn && mrn.trim())
  const matches = mrnMatches(typedMrn, mrn)
  const whenDate = fromLocalInput(when)
  const timeProblem = endTimeProblem(whenDate, { backtimeHours })
  const ready = matches && !timeProblem && !submitting

  const submit = async () => {
    if (!ready || !whenDate) return
    setSubmitting(true)
    setError(null)
    try {
      assertOnlineForWrite('record that the patient left the ward')
      setResult(await recordLeftWard(admissionId, {
        confirm_mrn: typedMrn.trim(),
        occurred_at: whenDate.toISOString(),
        ...(notes.trim() ? { notes: notes.trim() } : {}),
      }, key))
    } catch (err) {
      setError(isOfflineWriteRefused(err) ? err.message : clerkFailureWords(err))
    } finally {
      setSubmitting(false)
    }
  }

  if (result) {
    return (
      <section className="es" aria-labelledby={headingId} data-testid="left-ward-result">
        <h2 id={headingId} className="es__title">Left the ward</h2>
        <div className="es__box es__box--ok" role="status">
          <p className="es__box-text">
            {result.replayed
              ? 'This had already been recorded. Nothing new was written.'
              : `Recorded for ${formatClinicalDateTime(result.discharged_at)}.`}
          </p>
          <p className="es__box-text">
            No discharge summary was drafted and no patient link was issued. How the stay ended
            is not recorded here.
          </p>
          {result.bed_freed ? <p className="es__box-text">The bed is free for the next patient.</p> : null}
        </div>
        <div className="es__actions">
          <button type="button" className="es__btn es__btn--primary" onClick={onDone}>Done</button>
        </div>
      </section>
    )
  }

  const now = new Date()
  return (
    <section className="es" aria-labelledby={headingId} data-testid="left-ward">
      <h2 id={headingId} className="es__title">Left the ward</h2>
      <p className="es__lead">
        Records that this patient has left the ward, and when. Check the patient below against the
        wristband first.
      </p>

      <dl className="es__readback" aria-label="The patient">
        <div className="es__readback-item">
          <dt className="es__term">Name</dt>
          <dd className="es__value">{name || 'Not recorded'}</dd>
        </div>
        <div className="es__readback-item">
          <dt className="es__term">MRN</dt>
          <dd className="es__value">{mrnOnFile ? mrn : 'Not on file'}</dd>
        </div>
        <div className="es__readback-item">
          <dt className="es__term">Ward and bed</dt>
          <dd className="es__value">{place}</dd>
        </div>
      </dl>

      <div className="es__field">
        <label className="es__label" htmlFor={`${headingId}-when`}>When the patient left</label>
        <input
          id={`${headingId}-when`}
          className="es__input"
          type="datetime-local"
          value={when}
          min={toLocalInput(new Date(now.getTime() - backtimeHours * 3600 * 1000))}
          max={toLocalInput(now)}
          onChange={(e) => setWhen(e.target.value)}
          disabled={submitting}
          aria-invalid={!!timeProblem}
          aria-describedby={`${headingId}-when-state`}
        />
        <span id={`${headingId}-when-state`} className={timeProblem ? 'es__field-error' : 'es__hint'}>
          {timeProblem ?? `Up to ${backtimeHours} hours back, and not before the admission.`}
        </span>
      </div>

      <div className="es__field">
        <label className="es__label" htmlFor={`${headingId}-notes`}>Notes (optional)</label>
        <textarea
          id={`${headingId}-notes`}
          className="es__textarea"
          value={notes}
          maxLength={2000}
          onChange={(e) => setNotes(e.target.value)}
          disabled={submitting}
        />
      </div>

      <div className="es__field">
        <label className="es__label" htmlFor={`${headingId}-mrn`}>Type the MRN from the wristband to confirm</label>
        <input
          id={`${headingId}-mrn`}
          className="es__input es__input--mrn"
          value={typedMrn}
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => setTypedMrn(e.target.value)}
          disabled={submitting || !mrnOnFile}
          aria-invalid={typedMrn.trim() !== '' && !matches}
          aria-describedby={`${headingId}-mrn-state`}
        />
        <span
          id={`${headingId}-mrn-state`}
          className={matches ? 'es__field-ok' : (typedMrn.trim() ? 'es__field-error' : 'es__hint')}
        >
          {!mrnOnFile
            ? 'No MRN is on file for this patient, so this cannot be confirmed here. Ask the records desk to add it.'
            : matches
              ? 'The MRN matches.'
              : typedMrn.trim()
                ? 'This does not match the MRN on file.'
                : 'Type it exactly as the wristband shows it.'}
        </span>
      </div>

      {error && <p className="es__error-text" role="alert">{error}</p>}

      <div className="es__actions">
        <button type="button" className="es__btn" onClick={onClose} disabled={submitting}>Cancel</button>
        <button type="button" className="es__btn es__btn--primary" onClick={() => { void submit() }}
          disabled={!ready}>
          {submitting ? 'Recording' : 'Record left the ward'}
        </button>
      </div>
    </section>
  )
}
