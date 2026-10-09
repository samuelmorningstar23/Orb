/**
 * Bed board tile action: Reservation (adt-12).
 *
 * On a bed held by a reservation: what it is held for, until when, and for
 * whom when the caller may see that patient. Two things can be done here:
 *
 *  - PLACE the patient it is held for in the bed. An admission awaiting a bed
 *    is given it; a patient already in a bed is moved to it, keeping their
 *    treating department. The server fulfils the reservation in the same
 *    transaction. A bed not yet recorded clean takes the reason it is used
 *    anyway, and a boarder placement the acknowledgement, when the server
 *    asks for them; the retry sends the same Idempotency-Key.
 *  - RELEASE it, with a reason.
 *
 * Found by the board through ./index.ts; the board is not edited for it. No
 * actionPermission: adt.bed (the ward clerk, the administrator) and
 * patient.write (the doctors) both may; a role holding neither sees the
 * reservation and is told who acts on it. The server checks the patient is
 * inside the caller's department.
 */
import { useId, useState } from 'react'

import { toAdtError } from '../../../api/adt'
import { newWriteKey } from '../../../api/adt/endStay'
import {
  canReserve,
  expiryWords,
  placeHeldPatient,
  releaseReservation,
  reservationFailureWords,
  reservationOf,
} from '../../../api/adt/reservations'
import { assertOnlineForWrite, isOfflineWriteRefused } from '../../../lib/offline/writeGuard'
import '../EndStay.css'
import type { TileActionProps } from './index'

export const actionId = 'reservation'
export const actionLabel = 'Reservation'
export const actionOrder = 40
export const actionStatuses = 'reserved'

type Done = 'placed' | 'released' | null

export default function ReservationAction({ tile, ward, onDone, onClose }: TileActionProps) {
  const madeId = useId()
  const headingId = `reservation-${madeId}`
  const held = reservationOf(tile)
  const [placeKey] = useState(newWriteKey)
  const [reason, setReason] = useState('')
  const [notCleanReason, setNotCleanReason] = useState('')
  const [askNotClean, setAskNotClean] = useState(false)
  const [boarderWords, setBoarderWords] = useState<string | null>(null)
  const [boarderAck, setBoarderAck] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<Done>(null)
  const allowed = canReserve()

  if (!held) {
    return (
      <div className="es">
        <p className="es__note">This bed is no longer held. Reload the board.</p>
        <div className="es__actions">
          <button type="button" className="es__btn" onClick={onClose}>Close</button>
        </div>
      </div>
    )
  }

  const where = `Bed ${tile.bed_label ?? tile.unit_code} on ${ward.code}`
  const patient = held.patient
  const who = patient
    ? `${patient.name ?? 'Name not recorded'}${patient.mrn ? `, ${patient.mrn}` : ''}`
    : held.patient_hidden ? 'A patient outside your department' : 'No patient named yet'
  const placeable = allowed && !!patient?.admission_id

  const place = async () => {
    if (!patient?.admission_id || busy) return
    if (askNotClean && !notCleanReason.trim()) return
    if (boarderWords && !boarderAck) return
    setBusy(true)
    setError(null)
    try {
      assertOnlineForWrite('place the patient in this bed')
      await placeHeldPatient(patient.admission_id, tile.bed_id, {
        notCleanReason: askNotClean ? notCleanReason : undefined,
        boarderAcknowledged: boarderWords ? boarderAck : undefined,
      }, placeKey)
      setDone('placed')
    } catch (err) {
      if (isOfflineWriteRefused(err)) {
        setError(err.message)
      } else {
        const failure = toAdtError(err)
        const detail = (failure.detail ?? {}) as { message?: unknown }
        const words = typeof detail.message === 'string' ? detail.message : null
        if (failure.code === 'bed_not_clean_reason_required') {
          setAskNotClean(true)
          setError(words ?? 'This bed has not been recorded clean. Type the reason it is used anyway.')
        } else if (failure.code === 'boarder_acknowledgement_required') {
          setBoarderWords(words ?? 'This ward belongs to another department.')
          setError(null)
        } else {
          setError(reservationFailureWords(err))
        }
      }
    } finally {
      setBusy(false)
    }
  }

  const release = async () => {
    if (!reason.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      assertOnlineForWrite('release this reservation')
      await releaseReservation(held.id, reason)
      setDone('released')
    } catch (err) {
      setError(isOfflineWriteRefused(err) ? err.message : reservationFailureWords(err, 'release'))
    } finally {
      setBusy(false)
    }
  }

  if (done) {
    return (
      <section className="es" aria-labelledby={headingId} data-testid={`reservation-${done}`}>
        <h2 id={headingId} className="es__title">
          {done === 'placed' ? 'Patient placed' : 'Reservation released'}
        </h2>
        <div className="es__box es__box--ok" role="status">
          <p className="es__box-text">
            {done === 'placed'
              ? `${who} is now in ${where.charAt(0).toLowerCase()}${where.slice(1)}. The reservation is fulfilled.`
              : `${where} is no longer held.`}
          </p>
        </div>
        <div className="es__actions">
          <button type="button" className="es__btn es__btn--primary" onClick={() => onDone()}>
            Done
          </button>
        </div>
      </section>
    )
  }

  return (
    <section className="es" aria-labelledby={headingId} data-testid="reservation">
      <h2 id={headingId} className="es__title">{held.purpose_words}</h2>
      <dl className="es__readback" aria-label="The reservation">
        <div className="es__readback-item">
          <dt className="es__term">Bed</dt>
          <dd className="es__value">{where}</dd>
        </div>
        <div className="es__readback-item">
          <dt className="es__term">For</dt>
          <dd className="es__value">{who}</dd>
        </div>
        <div className="es__readback-item">
          <dt className="es__term">Until</dt>
          <dd className="es__value">{expiryWords(held.expires_at).replace(/^expires /, '') || 'Not recorded'}</dd>
        </div>
      </dl>
      <p className="es__note">Only the patient it is held for can be placed in this bed.</p>

      {!allowed && (
        <p className="es__note">A ward clerk, a doctor or the administrator acts on a reservation.</p>
      )}
      {allowed && patient && !patient.admission_id && (
        <p className="es__note">This patient is not admitted yet. Admit them first; then place them here.</p>
      )}

      {placeable && (
        <div className="es__box">
          <p className="es__box-title">Place {patient?.name ?? 'the patient'} in this bed</p>
          <p className="es__box-text">
            A patient awaiting a bed is given this one; a patient already in a bed is moved here
            and keeps their treating team. A move to another team is a transfer, on Move patient.
          </p>
          {askNotClean && (
            <div className="es__field">
              <label className="es__label" htmlFor={`${headingId}-clean`}>
                Why the bed is used before it is recorded clean
              </label>
              <textarea
                id={`${headingId}-clean`}
                className="es__textarea"
                value={notCleanReason}
                maxLength={2000}
                onChange={(e) => setNotCleanReason(e.target.value)}
                disabled={busy}
                aria-required="true"
              />
            </div>
          )}
          {boarderWords && (
            <label className="es__choice">
              <input
                type="checkbox"
                className="es__choice-input"
                checked={boarderAck}
                onChange={(e) => setBoarderAck(e.target.checked)}
                disabled={busy}
              />
              {boarderWords}
            </label>
          )}
          <div className="es__actions">
            <button
              type="button"
              className="es__btn es__btn--primary"
              onClick={() => { void place() }}
              disabled={busy || (askNotClean && !notCleanReason.trim()) || (!!boarderWords && !boarderAck)}
            >
              {busy ? 'Placing' : 'Place in this bed'}
            </button>
          </div>
        </div>
      )}

      {allowed && (
        <div className="es__field">
          <label className="es__label" htmlFor={`${headingId}-reason`}>Why release it</label>
          <textarea
            id={`${headingId}-reason`}
            className="es__textarea"
            value={reason}
            maxLength={500}
            onChange={(e) => setReason(e.target.value)}
            disabled={busy}
            aria-required="true"
          />
          <span className="es__hint">Required to release it. It is part of the record.</span>
        </div>
      )}

      {error && (
        <div className="es__box es__box--error" role="alert">
          <p className="es__error-text">{error}</p>
        </div>
      )}

      <div className="es__actions">
        <button type="button" className="es__btn" onClick={onClose} disabled={busy}>
          Close
        </button>
        {allowed && (
          <button
            type="button"
            className="es__btn"
            onClick={() => { void release() }}
            disabled={!reason.trim() || busy}
          >
            {busy ? 'Working' : 'Release reservation'}
          </button>
        )}
      </div>
    </section>
  )
}
