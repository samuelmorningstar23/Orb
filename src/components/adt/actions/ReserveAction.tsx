/**
 * Bed board tile action: Reserve bed (adt-12).
 *
 * Holds a free bed for a patient on the way: an admission, a transfer from
 * another ward, a patient coming back from surgery, a planned admission, or
 * other. A transfer and a patient coming back from surgery name the admitted
 * patient; for the others a patient may be named, or none yet. The bed is
 * held until the site's expiry (2 hours, 24 hours for a planned admission
 * and after surgery, ruling ADT-P9), and only the patient it is held for can
 * be placed in it. Found by the board through ./index.ts; the board is not
 * edited for it.
 *
 * The patients offered are the admitted patients the caller may see: those
 * lying in a bed on the caller's board and those awaiting a bed. Both reads
 * are the board's own (audited by the server, as the board is).
 *
 * Offered on beds nobody lies in and nobody holds (available, vacant,
 * waiting for or under a clean). No actionPermission: adt.bed (the ward
 * clerk, the administrator) and patient.write (the doctors) both reserve; a
 * role holding neither is told so, and the server refuses it anyway.
 */
import { useEffect, useId, useState } from 'react'

import { getAdtMeta } from '../../../api/adt'
import { getAwaitingBed, getBedBoard } from '../../../api/adt/board'
import { newWriteKey } from '../../../api/adt/endStay'
import {
  NEEDS_ADMISSION,
  PURPOSE_CHOICE_WORDS,
  RESERVATION_PURPOSES,
  canReserve,
  expiryWords,
  reservationFailureWords,
  reserveBed,
  type ReservationPurpose,
  type ReserveResult,
} from '../../../api/adt/reservations'
import { assertOnlineForWrite, isOfflineWriteRefused } from '../../../lib/offline/writeGuard'
import '../EndStay.css'
import type { TileActionProps } from './index'

export const actionId = 'reserve'
export const actionLabel = 'Reserve bed'
export const actionOrder = 40
export const actionStatuses = 'available,vacant_untracked,needs_cleaning,cleaning'

/** An admitted patient the caller may see, to hold the bed for. */
interface Choice {
  admission_id: string
  label: string
}

type Listing = { state: 'loading' } | { state: 'ready'; choices: Choice[] } | { state: 'failed' }

/** The site's default hold for a purpose, in minutes (ADT-P9). */
function holdMinutes(purpose: ReservationPurpose, settings: Record<string, unknown> | undefined): number {
  const long = purpose === 'elective' || purpose === 'post_op'
  const raw = Number(settings?.[long ? 'reservation_elective_minutes' : 'reservation_minutes'])
  return Number.isFinite(raw) && raw > 0 ? raw : (long ? 1440 : 120)
}

function holdWords(minutes: number): string {
  if (minutes % 60 === 0) {
    const hours = minutes / 60
    return hours === 1 ? '1 hour' : `${hours} hours`
  }
  return `${minutes} minutes`
}

export default function ReserveAction({ tile, ward, onDone, onClose }: TileActionProps) {
  const madeId = useId()
  const headingId = `reserve-${madeId}`
  const [key] = useState(newWriteKey)
  const [purpose, setPurpose] = useState<ReservationPurpose | null>(null)
  const [admissionId, setAdmissionId] = useState('')
  const [note, setNote] = useState('')
  const [listing, setListing] = useState<Listing>({ state: 'loading' })
  const [settings, setSettings] = useState<Record<string, unknown> | undefined>(undefined)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<ReserveResult | null>(null)
  const allowed = canReserve()

  useEffect(() => {
    if (!allowed) return
    let live = true
    Promise.resolve()
      .then(() => getAdtMeta())
      .then((meta) => { if (live) setSettings(meta?.settings) })
      .catch(() => { /* the defaults are shown; the server applies its own */ })
    Promise.resolve()
      .then(() => Promise.all([getBedBoard(null), getAwaitingBed(null)]))
      .then(([board, awaiting]) => {
        if (!live) return
        const seen = new Map<string, Choice>()
        for (const w of board.wards) {
          for (const t of w.tiles) {
            const o = t.occupant
            if (!o || seen.has(o.admission_id)) continue
            seen.set(o.admission_id, {
              admission_id: o.admission_id,
              label: `${o.name ?? 'Name not recorded'}${o.mrn ? `, ${o.mrn}` : ''} (${w.code} bed ${t.bed_label ?? t.unit_code})`,
            })
          }
        }
        for (const e of [...awaiting.awaiting, ...awaiting.unlinked]) {
          if (seen.has(e.admission_id)) continue
          seen.set(e.admission_id, {
            admission_id: e.admission_id,
            label: `${e.name ?? 'Name not recorded'}${e.mrn ? `, ${e.mrn}` : ''} (${e.bed ? `${e.ward ?? ''} bed ${e.bed}`.trim() : 'awaiting a bed'})`,
          })
        }
        setListing({ state: 'ready', choices: [...seen.values()] })
      })
      .catch(() => { if (live) setListing({ state: 'failed' }) })
    return () => { live = false }
  }, [allowed])

  if (!allowed) {
    return (
      <div className="es">
        <p className="es__note">Beds are reserved by a ward clerk, a doctor or the administrator.</p>
        <div className="es__actions">
          <button type="button" className="es__btn" onClick={onClose}>Close</button>
        </div>
      </div>
    )
  }

  const where = `bed ${tile.bed_label ?? tile.unit_code} on ${ward.code}`
  const needsPatient = !!purpose && NEEDS_ADMISSION.includes(purpose)
  const ready = !!purpose && (!needsPatient || !!admissionId) && !submitting

  const submit = async () => {
    if (!ready || !purpose) return
    setSubmitting(true)
    setError(null)
    try {
      assertOnlineForWrite('reserve this bed')
      setResult(await reserveBed({
        bed_id: tile.bed_id, purpose, admission_id: admissionId || undefined, note,
      }, key))
    } catch (err) {
      setError(isOfflineWriteRefused(err) ? err.message : reservationFailureWords(err))
    } finally {
      setSubmitting(false)
    }
  }

  if (result) {
    const res = result.reservation
    return (
      <section className="es" aria-labelledby={headingId} data-testid="reserve-result">
        <h2 id={headingId} className="es__title">Bed reserved</h2>
        <div className="es__box es__box--ok" role="status">
          <p className="es__box-text">
            {result.replayed ? 'This reservation had already been made. Nothing new was written. ' : ''}
            {`${res.purpose_words}: ${where}, ${expiryWords(res.expires_at)}.`}
          </p>
          <p className="es__box-text">
            Only the patient it is held for can be placed in it. Anyone with the bed board can
            release it.
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
    <section className="es" aria-labelledby={headingId} data-testid="reserve">
      <h2 id={headingId} className="es__title">Reserve {where}</h2>
      <p className="es__lead">
        Holds this bed for a patient on the way. Nobody else can be placed in it until it is
        released or its time runs out.
      </p>

      <fieldset className="es__fieldset">
        <legend className="es__legend">Held for</legend>
        <div className="es__choices">
          {RESERVATION_PURPOSES.map((p) => (
            <label key={p} className={`es__choice${purpose === p ? ' es__choice--on' : ''}`}>
              <input
                type="radio"
                className="es__choice-input"
                name={`${headingId}-purpose`}
                value={p}
                checked={purpose === p}
                onChange={() => setPurpose(p)}
                disabled={submitting}
              />
              {PURPOSE_CHOICE_WORDS[p]}
            </label>
          ))}
        </div>
        {purpose && (
          <span className="es__hint">
            Held for {holdWords(holdMinutes(purpose, settings))}, the site&apos;s setting.
          </span>
        )}
      </fieldset>

      <div className="es__field">
        <label className="es__label" htmlFor={`${headingId}-patient`}>
          {needsPatient ? 'Patient' : 'Patient (optional)'}
        </label>
        <select
          id={`${headingId}-patient`}
          className="es__input"
          value={admissionId}
          onChange={(e) => setAdmissionId(e.target.value)}
          disabled={submitting || listing.state !== 'ready'}
          aria-required={needsPatient ? 'true' : undefined}
        >
          <option value="">{needsPatient ? 'Choose the patient' : 'No patient named yet'}</option>
          {listing.state === 'ready' && listing.choices.map((c) => (
            <option key={c.admission_id} value={c.admission_id}>{c.label}</option>
          ))}
        </select>
        <span className={listing.state === 'failed' ? 'es__field-error' : 'es__hint'}>
          {listing.state === 'loading' && 'Loading your patients'}
          {listing.state === 'failed' && 'Your patients could not be loaded. A bed can still be held with no patient named, except for a transfer or after surgery.'}
          {listing.state === 'ready' && (needsPatient
            ? 'A transfer, or a patient coming back from surgery, is already admitted.'
            : 'A bed held for nobody named can be used only after it is released.')}
        </span>
      </div>

      <div className="es__field">
        <label className="es__label" htmlFor={`${headingId}-note`}>Note (optional)</label>
        <textarea
          id={`${headingId}-note`}
          className="es__textarea"
          value={note}
          maxLength={500}
          onChange={(e) => setNote(e.target.value)}
          disabled={submitting}
        />
      </div>

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
          {submitting ? 'Reserving' : 'Reserve bed'}
        </button>
      </div>
    </section>
  )
}
