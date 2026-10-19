/**
 * Bed board tile action: Carry out an ordered transfer (adt-17b, ruling
 * ADT-S8 (b), a build default for Samuel to confirm).
 *
 * The ward clerk never changes a patient's treating department on their own:
 * that changes who can open the chart. They may carry out a transfer a doctor
 * has ordered, naming that doctor (the patient's attending doctor or a doctor
 * of the patient's department; the server checks). The record says the clerk
 * named the doctor, never that the doctor acted, and the case room is told.
 *
 * The patient either stays in the bed they lie in (the usual case: the
 * receiving team takes over, and the patient may then board on this ward) or
 * leaves the bed to wait for one in the receiving department. Choosing a new
 * bed is a separate bed move. A boarder needs the words read and confirmed:
 * the server sends them, and the same request is sent again with the box
 * ticked (one Idempotency-Key per screen, so it is never recorded twice).
 *
 * Offered to the ward clerk only ('role:ward_clerk', which BedBoardPanel's
 * permission check answers from the signed-in role), on a tile with a patient
 * the clerk may see. ONE SCREEN PER STAY: keyed by the admission.
 */
import { useEffect, useId, useState } from 'react'

import { getMoveOptions } from '../../../api/adt/moves'
import {
  clerkFailureWords,
  getClerkAdmissionOptions,
  orderedTransfer,
  type OrderedTransferResult,
} from '../../../api/adt/clerk'
import { newWriteKey } from '../../../api/adt/endStay'
import { toAdtError } from '../../../api/adt'
import { assertOnlineForWrite, isOfflineWriteRefused } from '../../../lib/offline/writeGuard'
import '../EndStay.css'
import type { TileActionProps } from './index'

export const actionId = 'ordered-transfer'
export const actionLabel = 'Ordered transfer'
export const actionOrder = 25
export const actionPermission = 'role:ward_clerk'
export const actionStatuses = 'occupied,boarder'
export const actionNeedsOccupant = 1

export default function OrderedTransferAction({ tile, ward, onDone, onClose }: TileActionProps) {
  const occupant = tile.occupant
  if (!occupant) {
    return (
      <div className="es">
        <p className="es__note">Nobody you can see is in this bed, so there is nobody to transfer.</p>
        <div className="es__actions">
          <button type="button" className="es__btn" onClick={onClose}>Close</button>
        </div>
      </div>
    )
  }
  return (
    <OrderedTransferForm
      key={occupant.admission_id}
      admissionId={occupant.admission_id}
      name={occupant.name}
      department={occupant.department}
      expectedBedId={occupant.linked ? tile.bed_id : null}
      place={`${ward.code}, bed ${tile.bed_label ?? tile.unit_code}`}
      onDone={onDone}
      onClose={onClose}
    />
  )
}

interface OrderedTransferFormProps {
  admissionId: string
  name: string | null
  department: string | null
  expectedBedId: string | null
  place: string
  onDone: () => void
  onClose: () => void
}

type Lists =
  | { state: 'loading' }
  | { state: 'ready'; departments: string[]; doctors: { id: string; name: string }[] }
  | { state: 'failed'; message: string }

function OrderedTransferForm({
  admissionId, name, department, expectedBedId, place, onDone, onClose,
}: OrderedTransferFormProps) {
  const madeId = useId()
  const headingId = `ordered-transfer-${madeId}`
  const [key] = useState(newWriteKey)
  const [lists, setLists] = useState<Lists>({ state: 'loading' })
  const [toDepartment, setToDepartment] = useState('')
  const [orderedBy, setOrderedBy] = useState('')
  const [stays, setStays] = useState(true)
  const [reason, setReason] = useState('')
  const [boarderWords, setBoarderWords] = useState<string | null>(null)
  const [boarderAck, setBoarderAck] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<OrderedTransferResult | null>(null)

  useEffect(() => {
    let live = true
    Promise.all([getMoveOptions(), getClerkAdmissionOptions()])
      .then(([moves, clerk]) => {
        if (!live) return
        setLists({
          state: 'ready',
          departments: moves.departments.filter((d) => d !== department),
          doctors: clerk.doctors.map((d) => ({ id: d.id, name: d.name })),
        })
      })
      .catch((err) => {
        if (live) setLists({ state: 'failed', message: clerkFailureWords(err) })
      })
    return () => { live = false }
  }, [department])

  const ready = toDepartment !== '' && orderedBy !== '' && reason.trim() !== ''
    && (boarderWords === null || boarderAck) && !submitting

  const submit = async () => {
    if (!ready) return
    setSubmitting(true)
    setError(null)
    try {
      assertOnlineForWrite('record this transfer')
      setResult(await orderedTransfer({
        admission_id: admissionId,
        to_department: toDepartment,
        ordered_by: orderedBy,
        reason: reason.trim(),
        expected_bed_id: expectedBedId,
        ...(stays ? { keep_location: true } : { awaiting_bed: true }),
        ...(boarderAck ? { boarder_acknowledged: true } : {}),
      }, key))
    } catch (err) {
      const failure = toAdtError(err)
      if (failure.code === 'boarder_acknowledgement_required') {
        const words = (failure.detail as { message?: unknown } | null)?.message
        setBoarderWords(typeof words === 'string' ? words : failure.message)
        setBoarderAck(false)
      } else {
        setError(isOfflineWriteRefused(err) ? err.message : clerkFailureWords(err))
      }
    } finally {
      setSubmitting(false)
    }
  }

  if (result) {
    return (
      <section className="es" aria-labelledby={headingId} data-testid="ordered-transfer-result">
        <h2 id={headingId} className="es__title">Transfer recorded</h2>
        <div className="es__box es__box--ok" role="status">
          <p className="es__box-text">
            {result.replayed
              ? 'This transfer had already been recorded. Nothing new was written.'
              : `Now treated by ${result.current_department ?? toDepartment}, on the order of ${result.ordered_by?.name ?? 'the doctor named'}.`}
          </p>
          <p className="es__box-text">
            The patient leaves your board unless they stay on a ward of your department.
          </p>
        </div>
        <div className="es__actions">
          <button type="button" className="es__btn es__btn--primary" onClick={onDone}>Done</button>
        </div>
      </section>
    )
  }

  return (
    <section className="es" aria-labelledby={headingId} data-testid="ordered-transfer">
      <h2 id={headingId} className="es__title">Ordered transfer</h2>
      <p className="es__lead">
        Records a transfer to another department that a doctor has ordered. Name the doctor who
        ordered it; the record says you named them.
      </p>

      <dl className="es__readback" aria-label="The patient">
        <div className="es__readback-item">
          <dt className="es__term">Name</dt>
          <dd className="es__value">{name || 'Not recorded'}</dd>
        </div>
        <div className="es__readback-item">
          <dt className="es__term">Treated by</dt>
          <dd className="es__value">{department || 'Not recorded'}</dd>
        </div>
        <div className="es__readback-item">
          <dt className="es__term">Ward and bed</dt>
          <dd className="es__value">{place}</dd>
        </div>
      </dl>

      {lists.state === 'loading' && <p className="es__note" role="status">Loading the departments and doctors</p>}
      {lists.state === 'failed' && <p className="es__error-text" role="alert">{lists.message}</p>}

      {lists.state === 'ready' && (
        <>
          <div className="es__field">
            <label className="es__label" htmlFor={`${headingId}-to`}>Receiving department</label>
            <select id={`${headingId}-to`} className="es__input" value={toDepartment}
              onChange={(e) => { setToDepartment(e.target.value); setBoarderWords(null); setBoarderAck(false) }}
              disabled={submitting}>
              <option value="">Choose</option>
              {lists.departments.map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
          </div>

          <div className="es__field">
            <label className="es__label" htmlFor={`${headingId}-by`}>Ordered by</label>
            <select id={`${headingId}-by`} className="es__input" value={orderedBy}
              onChange={(e) => setOrderedBy(e.target.value)} disabled={submitting}>
              <option value="">Choose the doctor</option>
              {lists.doctors.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </select>
            <span className="es__hint">The patient&apos;s attending doctor or a doctor of {department || 'their department'}.</span>
          </div>

          <fieldset className="es__fieldset">
            <legend className="es__legend">Where the patient goes</legend>
            <div className="es__choices">
              <label className={`es__choice${stays ? ' es__choice--on' : ''}`}>
                <input type="radio" className="es__choice-input" name={`${headingId}-place`}
                  checked={stays} onChange={() => { setStays(true); setBoarderWords(null); setBoarderAck(false) }}
                  disabled={submitting} />
                Stays in this bed
              </label>
              <label className={`es__choice${!stays ? ' es__choice--on' : ''}`}>
                <input type="radio" className="es__choice-input" name={`${headingId}-place`}
                  checked={!stays} onChange={() => { setStays(false); setBoarderWords(null); setBoarderAck(false) }}
                  disabled={submitting} />
                Leaves this bed and waits for one
              </label>
            </div>
          </fieldset>

          <div className="es__field">
            <label className="es__label" htmlFor={`${headingId}-reason`}>Reason</label>
            <textarea id={`${headingId}-reason`} className="es__textarea" value={reason}
              maxLength={2000} onChange={(e) => setReason(e.target.value)} disabled={submitting} />
          </div>

          {boarderWords && (
            <div className="es__box es__box--warn">
              <p className="es__box-text">{boarderWords}</p>
              <label className="es__choice">
                <input type="checkbox" className="es__choice-input" checked={boarderAck}
                  onChange={(e) => setBoarderAck(e.target.checked)} disabled={submitting} />
                I have read this
              </label>
            </div>
          )}
        </>
      )}

      {error && <p className="es__error-text" role="alert">{error}</p>}

      <div className="es__actions">
        <button type="button" className="es__btn" onClick={onClose} disabled={submitting}>Cancel</button>
        <button type="button" className="es__btn es__btn--primary" onClick={() => { void submit() }}
          disabled={!ready}>
          {submitting ? 'Recording' : 'Record transfer'}
        </button>
      </div>
    </section>
  )
}
