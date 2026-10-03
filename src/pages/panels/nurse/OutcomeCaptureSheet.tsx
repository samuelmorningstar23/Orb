import { useCallback, useEffect, useRef, useState } from 'react'
import {
  confirmNoEvents,
  getShiftStatus,
  recordOutcome,
  type ShiftStatus,
  type ShiftWindow,
} from '../../../api/shadowOutcome'
import { getUser } from '../../../api/client'
import { assertOnlineForWrite } from '../../../lib/offline/writeGuard'
import { clockTime } from './time'
// The sheet owns its styling because it is composed outside this panel (the
// doctor reaches it from Vigil). Panels are lazy chunks: a control styled only
// by another panel's stylesheet renders unstyled until that tab is visited —
// the hazard ClinicianShell.css already calls out for the sort chips.
import './NursePanel.css'

/**
 * End-of-shift outcome capture — the charge nurse's half of the pilot's
 * evidence instrument (docs/OUTCOME_CAPTURE.md; server/routes/shadow_routes.py).
 *
 * Two writes, both ground truth:
 *
 *   1. "No deterioration events this shift" — once per ward-shift. Without it
 *      an unrecorded shift and an event-free shift are indistinguishable, and
 *      the ward-shift is EXCLUDED from every denominator as uncovered time.
 *      Confirming is an explicit two-step (press, then confirm the exact
 *      ward + shift statement); resubmitting an already-confirmed shift is a
 *      server-side no-op, never an error and never a second row.
 *
 *   2. A confirmed deterioration event (ICU transfer, code blue, …) — the
 *      ground-truth term of detection lead time, which is UNDEFINED without
 *      these rows. The form mirrors POST /outcome exactly; its vocabularies
 *      come from the shift-status response, served by the same module that
 *      validates the POST, so the form cannot offer a value the metric would
 *      discard.
 *
 * THE RECORDER MUST NEVER SEE SCORECARD NUMBERS (permissions.py:84-89): this
 * screen shows only what the shift owes — confirmation state and the count of
 * events the ward itself recorded. No rates, no metrics, nothing about how Orb
 * is scoring. That is a blinding boundary, not a styling choice; the server
 * enforces it (`outcome.record` cannot read the scorecard) and this component
 * must never grow a read that tests it.
 *
 * Observation-entry rule: never optimistic, never offline-queued. Every write
 * goes to the live server and every failure is shown loudly — a confirmation
 * the nurse believes was recorded but was not silently turns her event-free
 * shift into uncovered time.
 */

/** The only two fields the patient picker reads. Typed this narrowly on
 *  purpose: it is what lets the same sheet be composed into the doctor's Vigil
 *  board (whose rows are `api/client.Patient`, not `NursePatientCard`) without a
 *  second copy of the capture form. A `NursePatientCard` satisfies it. */
export interface OutcomePickerPatient {
  patient_id: string
  name: string
}

interface Props {
  /** Board patients, for the outcome form's patient picker. Optional: a
   *  deteriorated patient may already be off the board (ICU transfer), so the
   *  field always accepts a typed id too. */
  patients?: readonly OutcomePickerPatient[]
}

/** Fallbacks mirroring shadow_routes.py, for when shift-status is unreachable
 *  — the server still validates, so a stale list degrades to a 422, never to a
 *  wrong row. */
const FALLBACK_EVENT_TYPES = [
  'icu_transfer', 'rapid_response_call', 'code_blue', 'death',
  'unplanned_escalation_of_care',
]
const FALLBACK_SOURCES = ['witnessed', 'chart_review', 'handover']
const FALLBACK_RECOGNITION = ['clinician_independent', 'prompted_by_orb', 'unknown']

const EVENT_LABELS: Record<string, string> = {
  icu_transfer: 'ICU transfer',
  rapid_response_call: 'Rapid response call',
  code_blue: 'Code blue',
  death: 'Death',
  unplanned_escalation_of_care: 'Unplanned escalation of care',
}
const SOURCE_LABELS: Record<string, string> = {
  witnessed: 'Witnessed',
  chart_review: 'Chart review',
  handover: 'Handover',
}
const RECOGNITION_LABELS: Record<string, string> = {
  clinician_independent: 'Clinician, independently',
  prompted_by_orb: 'Prompted by Orb',
  unknown: 'Not known',
}

const label = (map: Record<string, string>, value: string) =>
  map[value] ?? value.replace(/_/g, ' ')

const shiftName = (shift: string) => shift.charAt(0).toUpperCase() + shift.slice(1)

const dayOf = (iso: string) =>
  new Date(iso).toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' })

export default function OutcomeCaptureSheet({ patients = [] }: Props) {
  // The signed-in user's department is the right default ward, but it is a
  // DEFAULT, not a scope: this is a statement of fact about a ward ("no events
  // on Ward B tonight"), entered by a named human, not a PHI read — an admin
  // backfilling, or a nurse covering two wards, legitimately names another.
  const [ward, setWard] = useState<string>(() => getUser()?.department ?? '')
  const [status, setStatus] = useState<ShiftStatus | null>(null)
  const [statusError, setStatusError] = useState('')
  const [loadingStatus, setLoadingStatus] = useState(false)
  const [statusVersion, setStatusVersion] = useState(0)

  const [confirmFor, setConfirmFor] = useState<string | null>(null) // period_start
  const [confirming, setConfirming] = useState(false)
  const confirmInFlightRef = useRef(false)
  const [confirmError, setConfirmError] = useState('')
  const [flash, setFlash] = useState('')

  const [showEventForm, setShowEventForm] = useState(false)
  const [patientId, setPatientId] = useState('')
  const [eventType, setEventType] = useState('')
  const [occurredAt, setOccurredAt] = useState('')
  const [source, setSource] = useState('witnessed')
  const [recognisedBy, setRecognisedBy] = useState('')
  const [note, setNote] = useState('')
  const [savingEvent, setSavingEvent] = useState(false)
  const eventInFlightRef = useRef(false)
  const [eventError, setEventError] = useState('')

  const reloadStatus = useCallback(() => setStatusVersion(v => v + 1), [])

  useEffect(() => {
    const name = ward.trim()
    if (!name) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
      setStatus(null)
      setStatusError('')
      return
    }
    let cancelled = false
    setLoadingStatus(true)
    // Debounced: the ward field re-queries as the nurse types.
    const timer = setTimeout(() => {
      getShiftStatus(name)
        .then(s => {
          if (cancelled) return
          setStatus(s)
          setStatusError('')
        })
        .catch(e => {
          if (cancelled) return
          setStatus(null)
          setStatusError(e instanceof Error ? e.message : 'Could not read the shift status')
        })
        .finally(() => { if (!cancelled) setLoadingStatus(false) })
    }, 350)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [ward, statusVersion])

  const submitConfirm = async (w: ShiftWindow) => {
    // Ref, not state: both taps of a double-tap read the same render's
    // `confirming === false`. Same latch as ObsEntrySheet.
    if (confirmInFlightRef.current) return
    confirmInFlightRef.current = true
    setConfirming(true)
    setConfirmError('')
    setFlash('')
    try {
      assertOnlineForWrite('confirm this shift')
      const res = await confirmNoEvents({
        ward: ward.trim(),
        period_start: w.period_start,
        period_end: w.period_end,
        shift: w.shift,
      })
      setConfirmFor(null)
      setFlash(res.already_confirmed
        ? `This shift was already confirmed${res.confirmed_at ? ` at ${clockTime(res.confirmed_at)}` : ''}${res.confirmed_by_role ? ` by the ${res.confirmed_by_role}` : ''}. Nothing further is owed.`
        : `Recorded: no deterioration events on ${res.ward}, ${shiftName(res.shift)} shift.`)
      reloadStatus()
    } catch (e) {
      // Loud, and honest about the consequence: an unrecorded confirmation is
      // uncovered time, not a retry-later inconvenience.
      setConfirmError(
        e instanceof Error
          ? e.message
          : 'The confirmation was NOT recorded. This ward-shift counts as uncovered time until it is.',
      )
    } finally {
      confirmInFlightRef.current = false
      setConfirming(false)
    }
  }

  const eventFormValid = patientId.trim() !== '' && eventType !== ''

  const submitEvent = async () => {
    if (eventInFlightRef.current || !eventFormValid) return
    eventInFlightRef.current = true
    setSavingEvent(true)
    setEventError('')
    setFlash('')
    try {
      assertOnlineForWrite('record this deterioration event')
      const res = await recordOutcome({
        patient_id: patientId.trim(),
        event_type: eventType,
        // datetime-local is the nurse's LOCAL wall clock; the server expects
        // ISO-8601 and reads a naive stamp as UTC, which on an Indian ward
        // would push every entry 5½ h into the future and get it rejected.
        ...(occurredAt ? { occurred_at: new Date(occurredAt).toISOString() } : {}),
        source,
        ...(recognisedBy ? { recognised_by: recognisedBy } : {}),
        ...(note.trim() ? { note: note.trim() } : {}),
      })
      setFlash(`Recorded: ${label(EVENT_LABELS, res.event_type)} for ${patientId.trim()} (${label(SOURCE_LABELS, res.source)}).`)
      setPatientId(''); setEventType(''); setOccurredAt(''); setSource('witnessed')
      setRecognisedBy(''); setNote('')
      setShowEventForm(false)
      reloadStatus()
    } catch (e) {
      setEventError(
        e instanceof Error
          ? e.message
          : 'The outcome was NOT recorded. Do not treat it as logged.',
      )
    } finally {
      eventInFlightRef.current = false
      setSavingEvent(false)
    }
  }

  const eventTypes = status?.event_types ?? FALLBACK_EVENT_TYPES
  const sources = status?.sources ?? FALLBACK_SOURCES
  const recognition = status?.recognition ?? FALLBACK_RECOGNITION

  const renderWindow = (w: ShiftWindow) => {
    const ws = w.wards[0]
    if (!ws) return null
    return (
      <section
        className={`shift-window ${w.is_current ? 'shift-window--current' : ''}`}
        key={w.period_start}
        aria-label={`${w.is_current ? 'Current' : 'Previous'} shift, ${shiftName(w.shift)}`}
      >
        <header className="shift-window__head">
          <h3 className="shift-window__title">{w.is_current ? 'Current shift' : 'Previous shift'}</h3>
          <span className="shift-window__meta">
            {shiftName(w.shift)} · {w.label} · {dayOf(w.period_start)}
          </span>
        </header>

        {ws.status === 'confirmed' && (
          <div className="nurse-note nurse-note--ok" role="status">
            Already confirmed, no deterioration events
            {ws.confirmed_by_role ? ` by the ${ws.confirmed_by_role}` : ''}
            {ws.confirmed_at ? ` at ${clockTime(ws.confirmed_at)}` : ''}.
            {' '}Nothing further is owed for this shift.
          </div>
        )}

        {ws.status === 'events_recorded' && (
          <div className="nurse-note nurse-note--info" role="status">
            {ws.events_recorded} deterioration event{ws.events_recorded === 1 ? '' : 's'} recorded
            this shift: the shift is covered by those records.
          </div>
        )}

        {ws.status === 'unknown' && (
          <div className="nurse-note nurse-note--warn" role="status">
            The confirmation history could not be read, so this shift's state is unknown.
            Confirming anyway is safe: a duplicate confirmation is deduplicated.
          </div>
        )}

        {(ws.status === 'outstanding' || ws.status === 'unknown') && (
          confirmFor === w.period_start ? (
            <div className="shift-confirm" role="group" aria-label="Confirm no deterioration events">
              <p className="shift-confirm__text">
                Confirm as ground truth: <strong>no deterioration events</strong> on{' '}
                <strong>{ward.trim()}</strong> during the {shiftName(w.shift).toLowerCase()} shift
                ({w.label}, {dayOf(w.period_start)}). Recorded once per shift, under your login.
              </p>
              <div className="shift-confirm__actions">
                <button
                  type="button"
                  className="nurse-btn nurse-btn--ghost"
                  onClick={() => setConfirmFor(null)}
                  disabled={confirming}
                >
                  Back
                </button>
                <button
                  type="button"
                  className="nurse-btn nurse-btn--primary"
                  onClick={() => void submitConfirm(w)}
                  disabled={confirming}
                >
                  {confirming ? 'Recording…' : 'Confirm: no events'}
                </button>
              </div>
            </div>
          ) : (
            <div>
              <button
                type="button"
                className="nurse-btn nurse-btn--primary"
                onClick={() => { setConfirmFor(w.period_start); setConfirmError(''); setFlash('') }}
              >
                No deterioration events this shift
              </button>
            </div>
          )
        )}
      </section>
    )
  }

  return (
    <div className="shift-capture">
      <p className="nurse-footnote shift-capture__why">
        {status?.why ??
          'A ward-shift with neither a recorded deterioration event nor a "no events" confirmation is excluded from the pilot\'s evidence as uncovered time. Silence is not evidence that nothing happened.'}
      </p>

      <div className="shift-capture__ward">
        <label className="shift-capture__label" htmlFor="shift-capture-ward">Ward</label>
        <input
          id="shift-capture-ward"
          className="shift-input"
          type="text"
          value={ward}
          maxLength={64}
          autoComplete="off"
          placeholder="Ward name"
          onChange={e => { setWard(e.target.value); setConfirmFor(null) }}
        />
      </div>

      {flash && <div className="nurse-note nurse-note--ok" role="status">{flash}</div>}
      {confirmError && <div className="nurse-note nurse-note--danger" role="alert">{confirmError}</div>}

      {ward.trim() === '' && (
        <p className="nurse-empty">Enter your ward to see what this shift still owes.</p>
      )}

      {statusError && ward.trim() !== '' && (
        <div className="nurse-note nurse-note--danger" role="alert">
          {statusError}{' '}
          <button type="button" className="nurse-btn nurse-btn--ghost nurse-btn--compact" onClick={reloadStatus}>
            Retry
          </button>
        </div>
      )}

      {loadingStatus && !status && ward.trim() !== '' && !statusError && (
        <p className="nurse-empty">Checking this shift…</p>
      )}

      {status && status.shifts.slice().reverse().map(renderWindow)}

      <section className="shift-capture__event">
        {showEventForm ? (
          <form
            className="outcome-form"
            aria-label="Record a deterioration event"
            onSubmit={e => { e.preventDefault(); void submitEvent() }}
          >
            <h3 className="shift-window__title">Record a deterioration event</h3>
            <p className="nurse-footnote">
              Ground truth for the pilot: an event that actually happened, whether or not
              anything alerted. Recorded against the patient, not the shift.
            </p>

            {eventError && <div className="nurse-note nurse-note--danger" role="alert">{eventError}</div>}

            <div className="outcome-form__grid">
              <div className="outcome-field">
                <label className="shift-capture__label" htmlFor="outcome-patient">Patient ID</label>
                <input
                  id="outcome-patient"
                  className="shift-input"
                  type="text"
                  value={patientId}
                  maxLength={64}
                  autoComplete="off"
                  list="outcome-patient-options"
                  placeholder="e.g. P0012"
                  onChange={e => setPatientId(e.target.value)}
                  disabled={savingEvent}
                />
                <datalist id="outcome-patient-options">
                  {patients.map(p => (
                    <option key={p.patient_id} value={p.patient_id}>{p.name}</option>
                  ))}
                </datalist>
                <span className="outcome-field__hint">
                  A transferred patient may already be off your board. Type the ID.
                </span>
              </div>

              <div className="outcome-field">
                <label className="shift-capture__label" htmlFor="outcome-event-type">Event</label>
                <select
                  id="outcome-event-type"
                  className="shift-input"
                  value={eventType}
                  onChange={e => setEventType(e.target.value)}
                  disabled={savingEvent}
                >
                  <option value="">Choose an event…</option>
                  {eventTypes.map(t => (
                    <option key={t} value={t}>{label(EVENT_LABELS, t)}</option>
                  ))}
                </select>
              </div>

              <div className="outcome-field">
                <label className="shift-capture__label" htmlFor="outcome-occurred-at">When it happened</label>
                <input
                  id="outcome-occurred-at"
                  className="shift-input"
                  type="datetime-local"
                  value={occurredAt}
                  onChange={e => setOccurredAt(e.target.value)}
                  disabled={savingEvent}
                />
                <span className="outcome-field__hint">
                  Leave blank for "now". Up to {status?.max_backdate_hours ?? 72} h back.
                </span>
              </div>

              <div className="outcome-field">
                <label className="shift-capture__label" htmlFor="outcome-recognised">How it was recognised</label>
                <select
                  id="outcome-recognised"
                  className="shift-input"
                  value={recognisedBy}
                  onChange={e => setRecognisedBy(e.target.value)}
                  disabled={savingEvent}
                >
                  <option value="">Not stated</option>
                  {recognition.map(r => (
                    <option key={r} value={r}>{label(RECOGNITION_LABELS, r)}</option>
                  ))}
                </select>
              </div>
            </div>

            <fieldset className="obs-segment">
              <legend className="obs-segment__legend">Source</legend>
              <div className="obs-segment__options" role="group">
                {sources.map(s => (
                  <button
                    key={s}
                    type="button"
                    className={`obs-chip ${source === s ? 'obs-chip--active' : ''}`}
                    aria-pressed={source === s}
                    onClick={() => setSource(s)}
                    disabled={savingEvent}
                  >
                    {label(SOURCE_LABELS, s)}
                  </button>
                ))}
              </div>
              {source === 'chart_review' && (
                <p className="outcome-field__hint">
                  Chart-review times run late and are reported separately. Use Witnessed when
                  someone actually saw it.
                </p>
              )}
            </fieldset>

            <div className="outcome-field">
              <label className="shift-capture__label" htmlFor="outcome-note">Note (optional)</label>
              <textarea
                id="outcome-note"
                className="shift-input"
                value={note}
                maxLength={1000}
                rows={3}
                onChange={e => setNote(e.target.value)}
                disabled={savingEvent}
              />
            </div>

            <div className="outcome-form__actions">
              <button
                type="button"
                className="nurse-btn nurse-btn--ghost"
                onClick={() => { setShowEventForm(false); setEventError('') }}
                disabled={savingEvent}
              >
                Cancel
              </button>
              <button
                type="submit"
                className="nurse-btn nurse-btn--primary"
                disabled={savingEvent || !eventFormValid}
                title={eventFormValid ? undefined : 'Enter the patient and the event type'}
              >
                {savingEvent ? 'Recording…' : 'Record event'}
              </button>
            </div>
          </form>
        ) : (
          <button
            type="button"
            className="nurse-btn nurse-btn--secondary"
            onClick={() => { setShowEventForm(true); setFlash('') }}
          >
            Record a deterioration event…
          </button>
        )}
      </section>
    </div>
  )
}
