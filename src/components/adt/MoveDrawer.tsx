/**
 * MoveDrawer: move an admitted patient (adt-6a). Two moves:
 *
 *   Change bed   another bed, the treating department kept (ruling ADT-S8:
 *                the ward clerk's adt.bed, and doctors). POST
 *                /api/adt/admissions/{id}/bed-change.
 *   Transfer     another treating department (adt.transfer, and doctors).
 *                POST /api/adt/transfers. To a bed on the receiving ward, to
 *                the ward with no bed yet, or staying in the current bed.
 *
 * Shown from the bed board's Move action and, while Beds and admissions is
 * on, from the front desk's Transfer button. It draws no overlay of its own:
 * the container does (inline under the bed board, a modal at the front desk).
 *
 * THE SERVER DECIDES, AS EVERYWHERE IN BEDS AND ADMISSIONS. The bed choice is
 * the admission form's BedPicker on the same lists (GET /api/adt/move-options,
 * no patient data): an occupied bed cannot be chosen and offers an extra bed
 * beside it (ADT-S1 (a)); a bed not recorded clean needs a reason and one
 * awaiting a terminal clean an authoriser who acts (ADT-S2 (c)); a ward of
 * another department makes the patient a boarder, said in words and
 * confirmed. The server checks all of it again and its words are printed.
 *
 * STAYING IN THE CURRENT PLACE is judged the same way. The location read's
 * kept_place is what the server's writer resolves that place to (a typed,
 * unlinked ward that matches a ward of bed setup included): a ward of
 * another department asks for the boarder confirmation, and a bed recorded
 * out of service for a reason, before the first press. Should the server
 * still name either on that path, the question opens here from its answer.
 * Every refusal is printed in the server's words: in the picker while the
 * picker is on screen, in the error box otherwise, never nowhere.
 *
 * A TRANSFER HANDS THE CHART OVER, and the screen says so before the button:
 * as soon as it is recorded the receiving department can open the record and
 * the sending one cannot. Only departments with clinical cover are offered.
 *
 * THE SCREEN'S OWN STATE. Where the patient lies is read when the screen
 * opens (GET /api/adt/admissions/{id}/location) and sent back as
 * expected_bed_id, so a move made by somebody else since is refused (409)
 * rather than overwritten. One Idempotency-Key per screen: a reply lost on
 * the ward network and a second press record the move once. A signing PIN
 * typed for a terminal-clean authorisation is cleared after every send.
 */
import { useCallback, useEffect, useId, useState } from 'react'

import {
  BED_REFUSAL_CODES,
  EMPTY_BED_CHOICE,
  bedChoiceMissing,
  changeBed,
  getAdmissionLocation,
  getMoveOptions,
  movePlacement,
  moveFailureWords,
  newIdempotencyKey,
  transferAdmission,
  type AdmissionLocation,
  type BedChoice,
  type MoveOptions,
  type MoveRequest,
  type MoveResult,
  type TransferRequest,
} from '../../api/adt/moves'
import { fromLocalInput, toLocalInput } from '../../api/adt/endStay'
import { toAdtError } from '../../api/adt'
import { assertOnlineForWrite, isOfflineWriteRefused } from '../../lib/offline/writeGuard'
import BedPicker from './BedPicker'
import '../AdmitPatientModal.css'
import './EndStay.css'

export type MoveMode = 'bed' | 'transfer'

export interface MovePatient {
  name: string | null
  mrn?: string | null
}

export interface MoveDrawerProps {
  admissionId: string
  patient: MovePatient
  /** May change the treating department (adt.transfer or patient.write). */
  canTransfer: boolean
  /** May change the bed (adt.bed or patient.write). */
  canChangeBed: boolean
  /** The move offered first. */
  initialMode?: MoveMode
  /** The id the container labels its dialog with; one is made otherwise. */
  titleId?: string
  /** A move was recorded and the person has read what happened. */
  onDone: (result: MoveResult) => void
  /** Closed before anything was recorded. */
  onClose: () => void
}

type Load<T> = { state: 'loading' } | { state: 'ready'; value: T } | { state: 'failed'; message: string }

type Destination = 'bed' | 'keep'

const FUTURE_SLACK_MS = 5 * 60 * 1000
const DEFAULT_BACKTIME_HOURS = 72

function place(ward?: string | null, bed?: string | null): string {
  const parts = [ward, bed ? `bed ${bed}` : null].filter(Boolean)
  return parts.length ? parts.join(', ') : 'No ward or bed recorded'
}

const same = (a?: string | null, b?: string | null) =>
  (a ?? '').trim().toLowerCase() === (b ?? '').trim().toLowerCase()

/** Why the move time cannot be sent, in words, or null. The server checks
 *  the same again. */
function timeProblem(when: Date | null, backtimeHours: number, now = new Date()): string | null {
  if (!when) return 'Enter the date and time the patient moved.'
  if (when.getTime() > now.getTime() + FUTURE_SLACK_MS) return 'The time of the move is in the future.'
  if (now.getTime() - when.getTime() > backtimeHours * 3600 * 1000) {
    return `A move can be recorded up to ${backtimeHours} hours back. Older entries need an administrator.`
  }
  return null
}

export default function MoveDrawer({
  admissionId, patient, canTransfer, canChangeBed, initialMode, titleId, onDone, onClose,
}: MoveDrawerProps) {
  const madeId = useId()
  const headingId = titleId ?? `move-${madeId}`
  const fieldId = `move-field-${madeId}`

  const [key] = useState(newIdempotencyKey)
  const firstMode: MoveMode = initialMode === 'transfer' && canTransfer ? 'transfer'
    : canChangeBed ? 'bed' : 'transfer'
  const [mode, setMode] = useState<MoveMode>(firstMode)
  const [options, setOptions] = useState<Load<MoveOptions>>({ state: 'loading' })
  const [location, setLocation] = useState<Load<AdmissionLocation>>({ state: 'loading' })
  const [toDepartment, setToDepartment] = useState('')
  const [destination, setDestination] = useState<Destination>('bed')
  const [keepAcknowledged, setKeepAcknowledged] = useState(false)
  const [keepOosReason, setKeepOosReason] = useState('')
  // What the server named on a send that kept the current place and the
  // location read did not foresee: a boarder (for the department sent, and
  // the ward's department when it said it) and a bed out of service.
  const [serverKeep, setServerKeep] = useState<{
    boarderFor?: string; wardDepartment?: string; outOfService?: boolean
  }>({})
  const [choice, setChoice] = useState<BedChoice>(EMPTY_BED_CHOICE)
  const [reason, setReason] = useState('')
  const [notes, setNotes] = useState('')
  const [opened] = useState(() => new Date())
  const [when, setWhen] = useState(() => toLocalInput(new Date()))
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<{ code: string; message: string } | null>(null)
  const [result, setResult] = useState<MoveResult | null>(null)

  // Both reads in the promise chain, so a failure of any kind is SAID. State
  // changes only in the replies' callbacks.
  const load = useCallback(() => {
    let live = true
    Promise.resolve()
      .then(() => getMoveOptions())
      .then((value) => { if (live) setOptions({ state: 'ready', value }) })
      .catch((err) => { if (live) setOptions({ state: 'failed', message: moveFailureWords(err) }) })
    Promise.resolve()
      .then(() => getAdmissionLocation(admissionId))
      .then((value) => { if (live) setLocation({ state: 'ready', value }) })
      .catch((err) => { if (live) setLocation({ state: 'failed', message: moveFailureWords(err) }) })
    return () => { live = false }
  }, [admissionId])

  useEffect(() => load(), [load])

  if (!canTransfer && !canChangeBed) {
    return (
      <section className="es" aria-labelledby={headingId}>
        <h2 id={headingId} className="es__title">Move patient</h2>
        <p className="es__note">Your role cannot move patients between beds or departments.</p>
        <div className="es__actions">
          <button type="button" className="es__btn" onClick={onClose}>Close</button>
        </div>
      </section>
    )
  }

  if (options.state !== 'ready' || location.state !== 'ready') {
    const failed = options.state === 'failed' ? options : location.state === 'failed' ? location : null
    return (
      <section className="es" aria-labelledby={headingId} aria-busy={!failed}>
        <h2 id={headingId} className="es__title">Move {patient.name || 'patient'}</h2>
        {failed ? (
          <div className="es__box es__box--error" role="alert">
            <p className="es__error-text">{failed.message}</p>
          </div>
        ) : (
          <p className="es__note" role="status">Reading the beds and where the patient lies</p>
        )}
        <div className="es__actions">
          <button type="button" className="es__btn" onClick={onClose}>Close</button>
          {failed && (
            <button
              type="button"
              className="es__btn"
              onClick={() => {
                setOptions({ state: 'loading' })
                setLocation({ state: 'loading' })
                load()
              }}
            >
              Try again
            </button>
          )}
        </div>
      </section>
    )
  }

  const { master, departments } = options.value
  const here = location.value
  const currentDept = here.department ?? ''
  const treating = mode === 'transfer' ? toDepartment : currentDept
  const handsOver = mode === 'transfer' && !!toDepartment && !same(toDepartment, currentDept)
  const keeping = mode === 'transfer' && destination === 'keep' && !!toDepartment
  // The current place as the server's writer resolves it (kept_place), the
  // linked ward's department when the server did not send it.
  const kept = here.kept_place ?? null
  const keptWard = kept?.ward ?? here.ward
  const keptBed = kept?.bed ?? here.bed
  const keptWardDept = serverKeep.wardDepartment ?? kept?.ward_department ?? here.ward_department
  const keepBoarder = keeping && (
    (!!keptWardDept && !same(keptWardDept, toDepartment))
    || (serverKeep.boarderFor !== undefined && same(serverKeep.boarderFor, toDepartment)))
  const keepOutOfService = keeping && (!!serverKeep.outOfService || !!kept?.out_of_service)
  const pickerShown = mode === 'bed' || (!!toDepartment && destination === 'bed')
  const backtimeHours = Number(master.backtime_hours) > 0 ? Number(master.backtime_hours)
    : DEFAULT_BACKTIME_HOURS
  const whenDate = fromLocalInput(when)
  const whenProblem = timeProblem(whenDate, backtimeHours)

  let missing: string | null = null
  if (mode === 'transfer' && !toDepartment) missing = 'Choose the receiving department.'
  else if (mode === 'transfer' && destination === 'keep') {
    if (keepBoarder && !keepAcknowledged) {
      missing = 'Confirm you have read who will be able to open this chart.'
    } else if (!handsOver) {
      missing = 'The patient is already treated by this department. Choose a new bed instead.'
    } else if (keepOutOfService && !keepOosReason.trim()) {
      missing = 'Type the reason the patient stays in a bed out of service.'
    }
  } else {
    missing = bedChoiceMissing(master, choice, treating)
    if (!missing && mode === 'bed' && choice.awaiting) {
      missing = 'Choose a bed. A patient waiting for a bed is given one, not moved.'
    }
  }
  if (!missing && !reason.trim()) missing = 'Give the reason for the move. It is part of the record.'
  if (!missing && whenProblem) missing = whenProblem
  const ready = !missing && !submitting

  const switchMode = (next: MoveMode) => {
    setMode(next)
    setChoice(EMPTY_BED_CHOICE)
    setDestination('bed')
    setKeepAcknowledged(false)
    setError(null)
  }

  const submit = async () => {
    if (!ready || !whenDate) return
    setSubmitting(true)
    setError(null)
    const base: MoveRequest = {
      expected_bed_id: here.bed_id ?? null,
      reason: reason.trim(),
    }
    // A time moved back from when the screen opened is sent; otherwise the
    // server stamps the move itself.
    if (Math.abs(whenDate.getTime() - opened.getTime()) >= 60 * 1000) {
      base.moved_at = whenDate.toISOString()
    }
    if (notes.trim()) base.notes = notes.trim()
    try {
      assertOnlineForWrite('record this move')
      if (mode === 'transfer') {
        const request: TransferRequest = {
          ...base,
          admission_id: admissionId,
          to_department: toDepartment,
          ...(destination === 'keep'
            ? {
                keep_location: true,
                ...(keepBoarder ? { boarder_acknowledged: true } : {}),
                ...(keepOutOfService ? { out_of_service_reason: keepOosReason.trim() } : {}),
              }
            : movePlacement(master, choice, { allowAwaiting: true })),
        }
        setResult(await transferAdmission(request, key))
      } else {
        setResult(await changeBed(admissionId,
          { ...base, ...movePlacement(master, choice, { allowAwaiting: false }) }, key))
      }
    } catch (err) {
      if (isOfflineWriteRefused(err)) {
        setError({ code: 'offline', message: err.message })
      } else {
        const failure = toAdtError(err)
        setError({ code: failure.code, message: moveFailureWords(err) })
        // Keeping the place, the server asked for what this screen can give:
        // open that question here, so its words are never a dead end.
        if (keeping && failure.code === 'boarder_acknowledgement_required') {
          const named = (failure.detail as { ward_department?: unknown } | null)?.ward_department
          setServerKeep((s) => ({
            ...s,
            boarderFor: toDepartment,
            ...(typeof named === 'string' && named.trim() ? { wardDepartment: named.trim() } : {}),
          }))
          setKeepAcknowledged(false)
        } else if (keeping && failure.code === 'out_of_service_reason_required') {
          setServerKeep((s) => ({ ...s, outOfService: true }))
        }
      }
    } finally {
      // The PIN is sent once and never kept.
      setChoice((c) => (c.terminalAuthoriserPin ? { ...c, terminalAuthoriserPin: '' } : c))
      setSubmitting(false)
    }
  }

  // ── What was recorded ───────────────────────────────────────────────────
  if (result) {
    const where = result.awaiting_bed
      ? `${result.current_ward || 'the ward'}, waiting for a bed`
      : place(result.current_ward, result.current_bed)
    return (
      <section className="es" aria-labelledby={headingId} data-testid="move-result">
        <h2 id={headingId} className="es__title">
          {result.replayed ? 'This move was already recorded' : 'Move recorded'}
        </h2>
        <div className="es__box es__box--ok" role="status">
          {result.replayed ? (
            <p className="es__box-text">
              Nothing was recorded a second time. Close this screen to see where the patient is now.
            </p>
          ) : (
            <>
              <p className="es__box-text">
                {result.department_changed
                  ? `${patient.name || 'The patient'} is now treated by ${result.current_department}.`
                  : `${patient.name || 'The patient'} has moved.`}
              </p>
              <p className="es__box-text">Now at {where}.</p>
              {result.boarder && (
                <p className="es__box-text">
                  The patient lies on a ward of another department, as a boarder.
                </p>
              )}
              {result.department_changed && (
                <p className="es__box-text">
                  Staff in {currentDept || 'the sending department'} can no longer open this chart.
                </p>
              )}
            </>
          )}
        </div>
        <div className="es__actions">
          <button type="button" className="es__btn es__btn--primary" onClick={() => onDone(result)}>
            Done
          </button>
        </div>
      </section>
    )
  }

  // ── The form ────────────────────────────────────────────────────────────
  // A refusal about the place is printed by the picker only while the picker
  // is on screen; otherwise (staying in the current place) by the error box.
  const pickerRefusal = pickerShown && error && BED_REFUSAL_CODES.has(error.code) ? error : null
  return (
    <section className="es" aria-labelledby={headingId} data-testid="move-drawer">
      <h2 id={headingId} className="es__title">Move {patient.name || 'patient'}</h2>

      <dl className="es__readback" aria-label="The patient">
        <div className="es__readback-item">
          <dt className="es__term">Name</dt>
          <dd className="es__value">{patient.name || 'Not recorded'}</dd>
        </div>
        {patient.mrn ? (
          <div className="es__readback-item">
            <dt className="es__term">MRN</dt>
            <dd className="es__value">{patient.mrn}</dd>
          </div>
        ) : null}
        <div className="es__readback-item">
          <dt className="es__term">Now at</dt>
          <dd className="es__value">
            {here.awaiting_bed ? `${here.ward || 'No ward'}, waiting for a bed` : place(here.ward, here.bed)}
          </dd>
        </div>
        <div className="es__readback-item">
          <dt className="es__term">Treated by</dt>
          <dd className="es__value">{currentDept || 'Not recorded'}</dd>
        </div>
      </dl>

      {canTransfer && canChangeBed && (
        <fieldset className="es__fieldset">
          <legend className="es__legend">What changes</legend>
          <div className="es__choices">
            <label className={`es__choice${mode === 'bed' ? ' es__choice--on' : ''}`}>
              <input
                type="radio"
                className="es__choice-input"
                name={`${fieldId}-mode`}
                checked={mode === 'bed'}
                onChange={() => switchMode('bed')}
                disabled={submitting}
              />
              <span>Change bed, same department</span>
            </label>
            <label className={`es__choice${mode === 'transfer' ? ' es__choice--on' : ''}`}>
              <input
                type="radio"
                className="es__choice-input"
                name={`${fieldId}-mode`}
                checked={mode === 'transfer'}
                onChange={() => switchMode('transfer')}
                disabled={submitting}
              />
              <span>Transfer to another department</span>
            </label>
          </div>
        </fieldset>
      )}

      {mode === 'transfer' && (
        <div className="es__field">
          <label className="es__label" htmlFor={`${fieldId}-dept`}>Receiving department</label>
          <select
            id={`${fieldId}-dept`}
            className="admit-input"
            value={toDepartment}
            onChange={(e) => { setToDepartment(e.target.value); setChoice(EMPTY_BED_CHOICE); setKeepAcknowledged(false) }}
            disabled={submitting}
          >
            <option value="">Select...</option>
            {departments.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
          <span className="es__hint">
            Only departments with clinical staff appear here. A department nobody covers would
            leave this patient admitted and on no one&apos;s board.
          </span>
        </div>
      )}

      {mode === 'transfer' && toDepartment && (
        <fieldset className="es__fieldset">
          <legend className="es__legend">Where the patient goes</legend>
          <div className="es__choices">
            <label className={`es__choice${destination === 'bed' ? ' es__choice--on' : ''}`}>
              <input
                type="radio"
                className="es__choice-input"
                name={`${fieldId}-dest`}
                checked={destination === 'bed'}
                onChange={() => setDestination('bed')}
                disabled={submitting}
              />
              <span>A bed on the receiving ward, or the ward with no bed yet</span>
            </label>
            <label className={`es__choice${destination === 'keep' ? ' es__choice--on' : ''}`}>
              <input
                type="radio"
                className="es__choice-input"
                name={`${fieldId}-dest`}
                checked={destination === 'keep'}
                onChange={() => setDestination('keep')}
                disabled={submitting}
              />
              <span>Stays in {here.bed ? `bed ${here.bed}` : 'the current place'} for now</span>
            </label>
          </div>
        </fieldset>
      )}

      {keepBoarder && (
        <div className="bed-picker__note bed-picker__note--warn" role="note">
          <p className="bed-picker__words">
            The patient stays on a ward of another department, as a boarder. Nurses on {keptWard || 'that ward'}{keptWardDept ? ` (${keptWardDept})` : ''} will
            not be able to open this chart. The {toDepartment} team keeps it.
          </p>
          <label className="bed-picker__check">
            <input
              type="checkbox"
              checked={keepAcknowledged}
              onChange={(e) => setKeepAcknowledged(e.target.checked)}
              disabled={submitting}
            />
            <span>I have read this. Keep the patient here as a boarder.</span>
          </label>
        </div>
      )}

      {keepOutOfService && (
        <div className="es__field">
          <label className="es__label" htmlFor={`${fieldId}-keep-oos`}>
            {keptBed ? `Bed ${keptBed}` : 'The current bed'} is recorded out of service by
            housekeeping. Reason the patient stays there
          </label>
          <input
            id={`${fieldId}-keep-oos`}
            className="es__input"
            value={keepOosReason}
            maxLength={2000}
            onChange={(e) => setKeepOosReason(e.target.value)}
            disabled={submitting}
            aria-required="true"
          />
        </div>
      )}

      {pickerShown && (
        <BedPicker
          master={master}
          department={treating}
          value={choice}
          onChange={setChoice}
          refusal={pickerRefusal}
          verb="move"
          allowNoBed={mode === 'transfer'}
        />
      )}

      <div className="es__field">
        <label className="es__label" htmlFor={`${fieldId}-reason`}>Reason for the move</label>
        <textarea
          id={`${fieldId}-reason`}
          className="es__textarea"
          value={reason}
          maxLength={2000}
          onChange={(e) => setReason(e.target.value)}
          disabled={submitting}
          aria-required="true"
        />
      </div>

      <div className="es__field">
        <label className="es__label" htmlFor={`${fieldId}-when`}>When the patient moved</label>
        <input
          id={`${fieldId}-when`}
          className="es__input"
          type="datetime-local"
          value={when}
          min={toLocalInput(new Date(opened.getTime() - backtimeHours * 3600 * 1000))}
          max={toLocalInput(new Date())}
          onChange={(e) => setWhen(e.target.value)}
          disabled={submitting}
          aria-invalid={!!whenProblem}
          aria-describedby={`${fieldId}-when-state`}
        />
        <span id={`${fieldId}-when-state`} className={whenProblem ? 'es__field-error' : 'es__hint'}>
          {whenProblem ?? `Up to ${backtimeHours} hours back, and not before the admission.`}
        </span>
      </div>

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

      {handsOver && (
        <div className="es__box es__box--warn" role="note" data-testid="move-handover">
          <p className="es__box-title">This hands the chart over.</p>
          <p className="es__box-text">
            As soon as the transfer is recorded, staff in {toDepartment} can open this
            patient&apos;s record and staff in {currentDept || 'the current department'} can no
            longer see them.
          </p>
        </div>
      )}

      {error && !pickerRefusal && (
        <div className="es__box es__box--error" role="alert">
          <p className="es__error-text">{error.message}</p>
        </div>
      )}

      {missing && !submitting && (
        <p className="es__hint" data-testid="move-missing">{missing}</p>
      )}

      <div className="es__actions">
        <button type="button" className="es__btn" onClick={onClose} disabled={submitting}>Cancel</button>
        <button type="button" className="es__btn es__btn--primary" onClick={submit} disabled={!ready}>
          {submitting ? 'Recording' : mode === 'transfer' ? 'Record transfer' : 'Record bed change'}
        </button>
      </div>
    </section>
  )
}
