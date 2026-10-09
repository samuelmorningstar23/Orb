/**
 * Bed board tile action: Swap beds (adt-6b).
 *
 * Two admitted patients exchange beds in one record: POST /api/adt/bed-swaps
 * writes two 'bed_swap' movements, each patient in the other's bed, and the
 * treating departments never change (ruling ADT-S8 held). Found by the board
 * through ./index.ts; the board is not edited for it.
 *
 * WHO. A bed manager or ward clerk (adt.bed), and doctors (patient.write).
 * BOTH patients must be ones the caller may see: the other patient is chosen
 * from the occupied beds the bed board names for this account, and the server
 * refuses (404) a swap with anybody else. Only patients in beds of bed setup
 * can swap; a typed bed is linked first.
 *
 * THE SCREEN'S STATE. The beds both tiles showed are sent as the expected
 * beds, so a move made since is refused (409) rather than undone. One
 * Idempotency-Key per screen covers the pair: a lost reply and a second press
 * swap once, never back. The screen is keyed by the stay in this file, so a
 * second bed gets a fresh screen and key.
 *
 * WHAT THE SERVER ASKS FOR opens here from its answer, in its words: the
 * boarder confirmation (a patient moved onto a ward of another department),
 * the reason neither bed is cleaned between the two patients on a ward whose
 * cleaning is tracked, an authoriser who acts for a bed whose next clean is
 * terminal, and the reason for a bed recorded out of service (ADT-S2 (c)).
 * A signing PIN is sent once and cleared.
 */
import { useCallback, useEffect, useId, useState } from 'react'

import { getBedBoard, type BedBoard, type BedOccupant, type BedTile } from '../../../api/adt/board'
import { fromLocalInput, toLocalInput } from '../../../api/adt/endStay'
import {
  authorisersFor,
  getMoveOptions,
  newIdempotencyKey,
  swapBeds,
  swapFailureWords,
  type MoveOptions,
  type SwapRequest,
  type SwapResult,
} from '../../../api/adt/moves'
import { toAdtError } from '../../../api/adt'
import { ADT_BED, PATIENT_WRITE, hasPermission } from '../../../lib/roles'
import { assertOnlineForWrite, isOfflineWriteRefused } from '../../../lib/offline/writeGuard'
import type { TileActionProps } from './index'
import '../../AdmitPatientModal.css'
import '../BedPicker.css'
import '../EndStay.css'

export const actionId = 'swap'
export const actionLabel = 'Swap beds'
export const actionOrder = 25
export const actionStatuses = 'occupied,occupied_on_leave,occupied_absconded,boarder'
export const actionNeedsOccupant = 1

const FUTURE_SLACK_MS = 5 * 60 * 1000
const DEFAULT_BACKTIME_HOURS = 72

const TERMINAL_CODES = new Set([
  'terminal_clean_authorisation_required', 'terminal_clean_pin_required',
  'terminal_clean_pin_wrong', 'terminal_clean_authoriser_locked',
  'terminal_clean_authoriser_no_pin', 'terminal_clean_authoriser_invalid',
  'terminal_clean_authoriser_out_of_department',
])

type Load<T> = { state: 'loading' } | { state: 'ready'; value: T } | { state: 'failed'; message: string }

interface Partner {
  tile: BedTile
  wardCode: string
}

interface Asks {
  boarderWords?: string
  notClean?: boolean
  outOfService?: boolean
  terminal?: boolean
}

function bedWords(wardCode: string, tile: BedTile): string {
  return `${wardCode}, bed ${tile.bed_label ?? tile.unit_code}`
}

/** The occupied beds of the board whose patient this account may see, linked
 *  to bed setup, other than `self`. */
function partnersOn(board: BedBoard, self: BedTile): Partner[] {
  const out: Partner[] = []
  for (const ward of board.wards) {
    for (const tile of ward.tiles) {
      if (tile.bed_id === self.bed_id || !tile.occupant || !tile.occupant.linked) continue
      if (tile.occupant.admission_id === self.occupant?.admission_id) continue
      out.push({ tile, wardCode: ward.code })
    }
  }
  return out
}

function timeProblem(when: Date | null, backtimeHours: number, now = new Date()): string | null {
  if (!when) return 'Enter the date and time the patients swapped.'
  if (when.getTime() > now.getTime() + FUTURE_SLACK_MS) return 'The time of the swap is in the future.'
  if (now.getTime() - when.getTime() > backtimeHours * 3600 * 1000) {
    return `A swap can be recorded up to ${backtimeHours} hours back. Older entries need an administrator.`
  }
  return null
}

export default function SwapAction({ tile, ward, onDone, onClose }: TileActionProps) {
  const occupant = tile.occupant
  const note = (words: string) => (
    <div className="es">
      <p className="es__note">{words}</p>
      <div className="es__actions">
        <button type="button" className="es__btn" onClick={onClose}>Close</button>
      </div>
    </div>
  )
  if (!occupant) return note('Nobody you can see is in this bed, so there is nobody to swap from here.')
  if (!hasPermission(ADT_BED) && !hasPermission(PATIENT_WRITE)) {
    return note('Your role cannot swap beds.')
  }
  if (!occupant.linked) {
    return note('This patient\'s bed is typed, not linked to bed setup. Link it under Awaiting a '
      + 'bed first; only patients in beds of bed setup can swap.')
  }
  return (
    <SwapDrawer
      key={occupant.admission_id}
      tile={tile}
      occupant={occupant}
      wardCode={ward.code}
      onDone={onDone}
      onClose={onClose}
    />
  )
}

interface SwapDrawerProps {
  tile: BedTile
  occupant: BedOccupant
  wardCode: string
  onDone: () => void
  onClose: () => void
}

function SwapDrawer({ tile, occupant, wardCode, onDone, onClose }: SwapDrawerProps) {
  const madeId = useId()
  const headingId = `swap-${madeId}`
  const fieldId = `swap-field-${madeId}`

  const [key] = useState(newIdempotencyKey)
  const [board, setBoard] = useState<Load<BedBoard>>({ state: 'loading' })
  const [options, setOptions] = useState<Load<MoveOptions>>({ state: 'loading' })
  const [partnerBed, setPartnerBed] = useState('')
  const [reason, setReason] = useState('')
  const [notes, setNotes] = useState('')
  const [opened] = useState(() => new Date())
  const [when, setWhen] = useState(() => toLocalInput(new Date()))
  const [asks, setAsks] = useState<Asks>({})
  const [boarderAck, setBoarderAck] = useState(false)
  const [notClean, setNotClean] = useState('')
  const [oos, setOos] = useState('')
  const [authoriser, setAuthoriser] = useState('')
  const [terminalReason, setTerminalReason] = useState('')
  const [pin, setPin] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<SwapResult | null>(null)

  // Both reads in the promise chain, so a failure of any kind is SAID.
  const load = useCallback(() => {
    let live = true
    Promise.resolve()
      .then(() => getBedBoard(null))
      .then((value) => { if (live) setBoard({ state: 'ready', value }) })
      .catch((err) => { if (live) setBoard({ state: 'failed', message: swapFailureWords(err) }) })
    Promise.resolve()
      .then(() => getMoveOptions())
      .then((value) => { if (live) setOptions({ state: 'ready', value }) })
      .catch((err) => { if (live) setOptions({ state: 'failed', message: swapFailureWords(err) }) })
    return () => { live = false }
  }, [])

  useEffect(() => load(), [load])

  if (board.state !== 'ready' || options.state !== 'ready') {
    const failed = board.state === 'failed' ? board : options.state === 'failed' ? options : null
    return (
      <section className="es" aria-labelledby={headingId} aria-busy={!failed}>
        <h2 id={headingId} className="es__title">Swap beds</h2>
        {failed ? (
          <div className="es__box es__box--error" role="alert">
            <p className="es__error-text">{failed.message}</p>
          </div>
        ) : (
          <p className="es__note" role="status">Reading the occupied beds you can see</p>
        )}
        <div className="es__actions">
          <button type="button" className="es__btn" onClick={onClose}>Close</button>
          {failed && (
            <button type="button" className="es__btn" onClick={() => {
              setBoard({ state: 'loading' })
              setOptions({ state: 'loading' })
              load()
            }}>
              Try again
            </button>
          )}
        </div>
      </section>
    )
  }

  const master = options.value.master
  const partners = partnersOn(board.value, tile)
  const partner = partners.find((p) => p.tile.bed_id === partnerBed) ?? null
  const backtimeHours = Number(master.backtime_hours) > 0 ? Number(master.backtime_hours)
    : DEFAULT_BACKTIME_HOURS
  const whenDate = fromLocalInput(when)
  const whenProblem = timeProblem(whenDate, backtimeHours)
  const authorisers = authorisersFor(master, null, occupant.department ?? '')
  const authorisesSelf = !!authoriser && authoriser === (master.self_authoriser?.id ?? null)

  let missing: string | null = null
  if (!partner) missing = 'Choose the patient to swap beds with.'
  else if (!reason.trim()) missing = 'Give the reason for the swap. It is part of the record.'
  else if (whenProblem) missing = whenProblem
  else if (asks.boarderWords && !boarderAck) missing = 'Confirm you have read who will be able to open each chart.'
  else if (asks.notClean && !notClean.trim()) missing = 'Type the reason neither bed is cleaned between the two patients.'
  else if (asks.outOfService && !oos.trim()) missing = 'Type the reason a patient goes into a bed out of service.'
  else if (asks.terminal && (!authoriser || !terminalReason.trim())) {
    missing = 'Choose who authorises a bed before its terminal clean, and why.'
  } else if (asks.terminal && !authorisesSelf && !pin) {
    missing = 'The person authorising types their own signing PIN.'
  }
  const ready = !missing && !submitting

  const submit = async () => {
    if (!ready || !partner?.tile.occupant || !whenDate) return
    setSubmitting(true)
    setError(null)
    const request: SwapRequest = {
      first_admission_id: occupant.admission_id,
      second_admission_id: partner.tile.occupant.admission_id,
      expected_first_bed_id: tile.bed_id,
      expected_second_bed_id: partner.tile.bed_id,
      reason: reason.trim(),
    }
    if (Math.abs(whenDate.getTime() - opened.getTime()) >= 60 * 1000) {
      request.moved_at = whenDate.toISOString()
    }
    if (notes.trim()) request.notes = notes.trim()
    if (asks.boarderWords && boarderAck) request.boarder_acknowledged = true
    if (asks.notClean && notClean.trim()) request.not_clean_reason = notClean.trim()
    if (asks.outOfService && oos.trim()) request.out_of_service_reason = oos.trim()
    if (asks.terminal && authoriser) {
      request.terminal_clean_authorised_by = authoriser
      request.terminal_clean_reason = terminalReason.trim()
      if (!authorisesSelf && pin) request.terminal_clean_authoriser_pin = pin
    }
    try {
      assertOnlineForWrite('record this swap')
      setResult(await swapBeds(request, key))
    } catch (err) {
      if (isOfflineWriteRefused(err)) {
        setError(err.message)
      } else {
        const failure = toAdtError(err)
        setError(swapFailureWords(err))
        if (failure.code === 'boarder_acknowledgement_required') {
          setAsks((s) => ({ ...s, boarderWords: failure.message }))
          setBoarderAck(false)
        } else if (failure.code === 'bed_not_clean_reason_required') {
          setAsks((s) => ({ ...s, notClean: true }))
        } else if (failure.code === 'out_of_service_reason_required') {
          setAsks((s) => ({ ...s, outOfService: true }))
        } else if (TERMINAL_CODES.has(failure.code)) {
          setAsks((s) => ({ ...s, terminal: true }))
        }
      }
    } finally {
      // The PIN is sent once and never kept.
      setPin('')
      setSubmitting(false)
    }
  }

  if (result) {
    const first = result.first
    const second = result.second
    const where = (r: typeof first) => [r.current_ward, r.current_bed ? `bed ${r.current_bed}` : null]
      .filter(Boolean).join(', ')
    return (
      <section className="es" aria-labelledby={headingId} data-testid="swap-result">
        <h2 id={headingId} className="es__title">
          {result.replayed ? 'This swap was already recorded' : 'Beds swapped'}
        </h2>
        <div className="es__box es__box--ok" role="status">
          {result.replayed ? (
            <p className="es__box-text">
              Nothing was recorded a second time. Close this screen to see where the patients are now.
            </p>
          ) : (
            <>
              <p className="es__box-text">
                {occupant.name || 'The first patient'} is now at {where(first)}.
              </p>
              <p className="es__box-text">
                {partner?.tile.occupant?.name || 'The second patient'} is now at {where(second)}.
              </p>
              {(first.boarder || second.boarder) && (
                <p className="es__box-text">
                  A patient now lies on a ward of another department, as a boarder.
                </p>
              )}
            </>
          )}
        </div>
        <div className="es__actions">
          <button type="button" className="es__btn es__btn--primary" onClick={onDone}>Done</button>
        </div>
      </section>
    )
  }

  return (
    <section className="es" aria-labelledby={headingId} data-testid="swap-drawer">
      <h2 id={headingId} className="es__title">Swap beds</h2>

      <dl className="es__readback" aria-label="The patient">
        <div className="es__readback-item">
          <dt className="es__term">Name</dt>
          <dd className="es__value">{occupant.name || 'Not recorded'}</dd>
        </div>
        {occupant.mrn ? (
          <div className="es__readback-item">
            <dt className="es__term">MRN</dt>
            <dd className="es__value">{occupant.mrn}</dd>
          </div>
        ) : null}
        <div className="es__readback-item">
          <dt className="es__term">Now at</dt>
          <dd className="es__value">{bedWords(wardCode, tile)}</dd>
        </div>
      </dl>

      <div className="es__field">
        <label className="es__label" htmlFor={`${fieldId}-with`}>Swap with</label>
        <select
          id={`${fieldId}-with`}
          className="admit-input"
          value={partnerBed}
          onChange={(e) => { setPartnerBed(e.target.value); setAsks({}); setError(null) }}
          disabled={submitting}
        >
          <option value="">Select...</option>
          {partners.map((p) => (
            <option key={p.tile.bed_id} value={p.tile.bed_id}>
              {bedWords(p.wardCode, p.tile)}: {p.tile.occupant?.name || 'Name not recorded'}
              {p.tile.occupant?.mrn ? ` (${p.tile.occupant.mrn})` : ''}
            </option>
          ))}
        </select>
        <span className="es__hint">
          {partners.length
            ? 'Only patients you can see, in beds of bed setup, are listed. Their treating teams do not change.'
            : 'No other patient you can see lies in a bed of bed setup.'}
        </span>
      </div>

      {asks.boarderWords && (
        <div className="bed-picker__note bed-picker__note--warn" role="note">
          <p className="bed-picker__words">{asks.boarderWords}</p>
          <label className="bed-picker__check">
            <input type="checkbox" checked={boarderAck} disabled={submitting}
              onChange={(e) => setBoarderAck(e.target.checked)} />
            <span>I have read this. Swap the beds with the patient as a boarder there.</span>
          </label>
        </div>
      )}

      {asks.notClean && (
        <div className="es__field">
          <label className="es__label" htmlFor={`${fieldId}-clean`}>
            Neither bed is cleaned between the two patients. Reason they swap first
          </label>
          <input id={`${fieldId}-clean`} className="es__input" value={notClean} maxLength={2000}
            onChange={(e) => setNotClean(e.target.value)} disabled={submitting} aria-required="true" />
        </div>
      )}

      {asks.outOfService && (
        <div className="es__field">
          <label className="es__label" htmlFor={`${fieldId}-oos`}>
            A bed is recorded out of service by housekeeping. Reason a patient goes into it
          </label>
          <input id={`${fieldId}-oos`} className="es__input" value={oos} maxLength={2000}
            onChange={(e) => setOos(e.target.value)} disabled={submitting} aria-required="true" />
        </div>
      )}

      {asks.terminal && (
        <fieldset className="es__fieldset">
          <legend className="es__legend">A bed is waiting for a terminal clean</legend>
          <div className="es__field">
            <label className="es__label" htmlFor={`${fieldId}-auth`}>Who authorises it</label>
            <select id={`${fieldId}-auth`} className="admit-input" value={authoriser}
              onChange={(e) => { setAuthoriser(e.target.value); setPin('') }} disabled={submitting}>
              <option value="">Select...</option>
              {authorisers.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.id === master.self_authoriser?.id ? `${a.name} (you)` : a.name}
                </option>
              ))}
            </select>
          </div>
          {authoriser && !authorisesSelf && (
            <div className="es__field">
              <label className="es__label" htmlFor={`${fieldId}-pin`}>Their signing PIN, typed by them</label>
              <input id={`${fieldId}-pin`} className="es__input" type="password" inputMode="numeric"
                autoComplete="off" value={pin} onChange={(e) => setPin(e.target.value)}
                disabled={submitting} />
            </div>
          )}
          <div className="es__field">
            <label className="es__label" htmlFor={`${fieldId}-term`}>
              Why the bed is used before its terminal clean
            </label>
            <input id={`${fieldId}-term`} className="es__input" value={terminalReason} maxLength={2000}
              onChange={(e) => setTerminalReason(e.target.value)} disabled={submitting} />
          </div>
        </fieldset>
      )}

      <div className="es__field">
        <label className="es__label" htmlFor={`${fieldId}-reason`}>Reason for the swap</label>
        <textarea id={`${fieldId}-reason`} className="es__textarea" value={reason} maxLength={2000}
          onChange={(e) => setReason(e.target.value)} disabled={submitting} aria-required="true" />
      </div>

      <div className="es__field">
        <label className="es__label" htmlFor={`${fieldId}-when`}>When the patients swapped</label>
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
        />
      </div>

      <div className="es__field">
        <label className="es__label" htmlFor={`${fieldId}-notes`}>Notes (optional)</label>
        <textarea id={`${fieldId}-notes`} className="es__textarea" value={notes} maxLength={2000}
          onChange={(e) => setNotes(e.target.value)} disabled={submitting} />
      </div>

      {error && (
        <div className="es__box es__box--error" role="alert">
          <p className="es__error-text">{error}</p>
        </div>
      )}

      {missing && !submitting && <p className="es__hint" data-testid="swap-missing">{missing}</p>}

      <div className="es__actions">
        <button type="button" className="es__btn" onClick={onClose} disabled={submitting}>Cancel</button>
        <button type="button" className="es__btn es__btn--primary" onClick={() => { void submit() }}
          disabled={!ready}>
          {submitting ? 'Recording' : 'Record swap'}
        </button>
      </div>
    </section>
  )
}
