import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  previewNews2,
  recordObservations,
  type IllegibleField,
  type News2,
  type NursePatientCard,
  type ObsSchedule,
  type ObservationInput,
} from '../../../api/nurse'
import { getUser } from '../../../api/client'
import { hasPermission, VITALS_TRANSCRIBE, VITALS_WRITE } from '../../../lib/roles'
import { useClinicalSwitches } from '../../../shellConfig'
import { useFocusTrap } from '../../../hooks/useFocusTrap'
import { useNow } from '../../../hooks/useNow'
import { isOwnFreshDraft, obsDraftKey, type DraftEnvelope } from '../../../lib/nurseDrafts'
import { assertOnlineForWrite } from '../../../lib/offline/writeGuard'
import type { OfflineSnapshot } from '../../../hooks/useOfflineSnapshot'
import OfflineBanner from '../../../components/OfflineBanner'
import News2Badge from '../../../components/News2Badge'

/**
 * Bedside observation entry, built for a tablet at 3 a.m.
 *
 * Three things this screen has to get right:
 *
 *  1. SPEED — one screen, no paging, no dropdown that needs a mouse. Every
 *     field is a big numeric target with inputMode="decimal" so the tablet
 *     raises a number pad, and ACVPU / oxygen are segmented buttons rather than
 *     <select>s. Tab order follows the order obs are physically taken.
 *
 *  2. THE SCORE, IMMEDIATELY — the clinical point of taking obs is the NEWS2
 *     they produce, so it is computed live as the nurse types (debounced,
 *     server-side, from the same compute_news2 the save path uses) along with
 *     the RCP escalation response for that band. No local re-implementation of
 *     the scoring table: a preview that could disagree with the recorded score
 *     is worse than no preview.
 *
 *  3. NEVER LOSE A READING — a nurse mid-obs gets interrupted constantly. Every
 *     keystroke is mirrored to localStorage, restored on reopen, and closing
 *     with unsaved values asks first. The draft is cleared only after the
 *     server confirms the write.
 *
 *     That draft is scoped to the SESSION, not to the device (see
 *     lib/nurseDrafts). This runs on a tablet that is shared by a whole ward:
 *     without scoping, the next nurse to pick it up could read the previous
 *     nurse's in-progress observations for a named patient — PHI surviving the
 *     session on shared hardware. The draft is stamped with its owner, refused
 *     to anyone else, aged out after a shift, and wiped on logout. Within one
 *     nurse's session nothing changes: their interrupted obs still come back.
 *
 *  4. ONE TAP, ONE OBSERVATION — a tablet double-tap must not chart two sets of
 *     obs. The server dedupes on (patient, source, taken_at) by EXACT timestamp
 *     equality, so `taken_at` is stamped ONCE per sheet (a ref, not state) and
 *     re-used by every submit; a second tap therefore lands on the identical
 *     timestamp and is recognised as a replay instead of charted twice. The
 *     in-flight guard is also a ref, because two taps 6 ms apart both run
 *     against the same render's `saving` state and a state-based guard sees
 *     `false` on both.
 *
 *  5. THE CLERK'S COPY (Orb Ward Record, 6 October 2026). An account that
 *     copies sets from the paper sheet (vitals.transcribe, no vitals.write)
 *     gets a required "Time on the sheet", which becomes `taken_at` and may be
 *     days back but never in the future; an "Illegible" tick beside every
 *     reading, which blanks it and names it in `illegible`; and no score at
 *     all, live or after saving, because the record copies the sheet and
 *     scores nothing. With scoring switched off on this computer
 *     (useClinicalSwitches().scoringEnabled false) nobody sees a score.
 *
 *  6. NOTHING IS PRESELECTED (9 October 2026). ACVPU and air or oxygen start
 *     unselected. A box left empty is sent empty and stored empty, never as
 *     alert and never as on air, so the rounds file can count it as blank and
 *     the score says the set is incomplete. A nurse at the bedside must choose
 *     both before she can save (the server refuses her set otherwise). The
 *     clerk also gets "Not recorded on the sheet", and a box she leaves empty
 *     is sent empty.
 */

/** What the sheet needs to know about the patient. The shift board passes its
 *  whole card; the clerk's Observations view has no chart, so it passes the
 *  bed-board identity only. */
export type ObsSheetPatient =
  Pick<NursePatientCard, 'patient_id' | 'name' | 'bed' | 'mrn'>
  & Partial<Pick<NursePatientCard, 'allergies' | 'on_scale_2'>>

interface Props {
  patient: ObsSheetPatient
  /**
   * The shift board's snapshot, when the caller has one. Optional because the
   * refusal in save() — not this label — is what actually stops an offline
   * write; a caller without a snapshot loses the early warning, never the
   * guarantee.
   */
  offline?: OfflineSnapshot<unknown>
  onClose: () => void
  /** news2 and schedule are null when no score is computed: the clerk's copy,
   *  or scoring switched off on this computer. `message` is passed only when
   *  the server said something about the set, e.g. that a copy older than 2
   *  hours was charted without raising an alert. */
  onRecorded: (news2: News2 | null, schedule: ObsSchedule | null, message?: string) => void
}

type FieldKey =
  | 'respiratory_rate' | 'spo2' | 'systolic_bp' | 'diastolic_bp'
  | 'heart_rate' | 'temperature'

interface FieldSpec {
  key: FieldKey
  label: string
  unit: string
  min: number
  max: number
  step: string
  /** NEWS2 breakdown parameter name(s) as compute_news2 reports them, for the
   *  per-field points. SpO2 has two: the row is named after the table that
   *  scored it, "SpO2 (Scale 2)" for a patient on Scale 2. Matching "SpO2" alone
   *  hid the chip on exactly the patients whose SpO2 points differ. */
  scoreParams: string[]
}

// Ordered the way a set of obs is actually taken at the bedside, which is also
// the NEWS2 chart order — so the tab sequence matches the nurse's hands.
const FIELDS: FieldSpec[] = [
  { key: 'respiratory_rate', label: 'Respiratory rate', unit: '/min', min: 2, max: 80, step: '1', scoreParams: ['Respiratory rate'] },
  { key: 'spo2', label: 'Oxygen saturation', unit: '%', min: 30, max: 100, step: '1', scoreParams: ['SpO2', 'SpO2 (Scale 2)'] },
  { key: 'systolic_bp', label: 'Systolic BP', unit: 'mmHg', min: 40, max: 300, step: '1', scoreParams: ['Systolic BP'] },
  { key: 'diastolic_bp', label: 'Diastolic BP', unit: 'mmHg', min: 10, max: 200, step: '1', scoreParams: [] },
  { key: 'heart_rate', label: 'Heart rate', unit: 'bpm', min: 10, max: 300, step: '1', scoreParams: ['Heart rate'] },
  { key: 'temperature', label: 'Temperature', unit: '°C', min: 25, max: 45, step: '0.1', scoreParams: ['Temperature'] },
]

const ACVPU: { value: string; label: string; hint: string }[] = [
  { value: 'alert', label: 'Alert', hint: 'A' },
  { value: 'confusion', label: 'Confusion', hint: 'C' },
  { value: 'voice', label: 'Voice', hint: 'V' },
  { value: 'pain', label: 'Pain', hint: 'P' },
  { value: 'unresponsive', label: 'Unresponsive', hint: 'U' },
]

/** The clerk's answer for a box the paper sheet left empty. Sent as null. */
const NOT_RECORDED = 'not-recorded'

type OxygenChoice = 'air' | 'oxygen' | typeof NOT_RECORDED

const OXYGEN: { value: OxygenChoice; label: string }[] = [
  { value: 'air', label: 'On air' },
  { value: 'oxygen', label: 'On oxygen' },
]

type DraftValues = Record<FieldKey, string>

const EMPTY: DraftValues = {
  respiratory_rate: '', spo2: '', systolic_bp: '', diastolic_bp: '',
  heart_rate: '', temperature: '',
}

interface Draft extends DraftEnvelope {
  values: DraftValues
  /** Null until chosen. Read back only from a draft that says `choices: 2`:
   *  an older draft's alert and on air were the screen's preselection, not a
   *  choice anyone made, so they come back unselected. */
  consciousness?: string | null
  oxygen?: OxygenChoice | null
  choices?: 2
  savedAt: number
  /** The clerk's ticks and sheet time, so an interrupted copy comes back whole. */
  illegible?: IllegibleField[]
  sheetTime?: string
}

/** `datetime-local` value for a Date, in the computer's own time zone. */
function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
    + `T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** A `datetime-local` value read as local time; null when it is not one. */
function fromLocalInput(value: string): Date | null {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) return null
  const d = new Date(value)
  return Number.isNaN(d.getTime()) ? null : d
}

/**
 * Restore this nurse's own interrupted draft — and nobody else's.
 *
 * A draft belonging to another user, or one older than a shift, is not just
 * ignored: it is REMOVED. Leaving it on disk would keep another nurse's PHI on
 * a shared tablet indefinitely, silently, for anyone with devtools.
 */
function readDraft(patientId: string, ownerId: string | null): Draft | null {
  const key = obsDraftKey(patientId)
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Draft
    if (!parsed || typeof parsed !== 'object' || !parsed.values) {
      localStorage.removeItem(key)
      return null
    }
    if (!isOwnFreshDraft(parsed, ownerId)) {
      localStorage.removeItem(key)
      return null
    }
    const chosen = parsed.choices === 2
    const cons = chosen ? parsed.consciousness ?? null : null
    const oxygen = chosen ? parsed.oxygen ?? null : null
    return {
      ...parsed,
      values: { ...EMPTY, ...parsed.values },
      consciousness: cons === NOT_RECORDED || ACVPU.some(a => a.value === cons) ? cons : null,
      oxygen: oxygen === NOT_RECORDED || OXYGEN.some(o => o.value === oxygen) ? oxygen : null,
    }
  } catch {
    return null
  }
}

export default function ObsEntrySheet({ patient, offline, onClose, onRecorded }: Props) {
  // Who this draft belongs to. Read from the stored session rather than the
  // AuthContext so this leaf does not import App (api/client keeps that
  // direction one-way on purpose); it is the same identity, set at login and
  // cleared by clearToken() at logout.
  const [ownerId] = useState<string | null>(() => getUser()?.id ?? null)

  // Read any interrupted draft exactly once, in a lazy state initializer, so the
  // restore is part of the first render rather than a post-mount effect that
  // would briefly show the nurse an empty form they might start retyping.
  const [restored] = useState<Draft | null>(() => readDraft(patient.patient_id, ownerId))
  const [values, setValues] = useState<DraftValues>(restored?.values ?? EMPTY)
  // Nothing preselected: null until a choice is made (point 6 above).
  const [consciousness, setConsciousness] = useState<string | null>(restored?.consciousness ?? null)
  const [oxygen, setOxygen] = useState<OxygenChoice | null>(restored?.oxygen ?? null)
  const [showRestoredNote, setShowRestoredNote] = useState(!!restored)
  const restoredAt = restored?.savedAt ?? null

  // The clerk copies the paper sheet (vitals.transcribe without vitals.write).
  // Decided once from the stored session, like ownerId above.
  const [transcribing] = useState(
    () => hasPermission(VITALS_TRANSCRIBE) && !hasPermission(VITALS_WRITE),
  )
  const { scoringEnabled } = useClinicalSwitches()
  // No score for the clerk's copy, and none for anyone while scoring is off.
  const showScore = scoringEnabled && !transcribing
  const [illegible, setIllegible] = useState<ReadonlySet<IllegibleField>>(
    () => new Set(restored?.illegible ?? []),
  )
  const [sheetTime, setSheetTime] = useState<string>(
    () => restored?.sheetTime ?? toLocalInput(new Date()),
  )
  // The clock for "never in the future", read through useNow so the render
  // stays pure. Ticks only on the clerk's copy.
  const now = useNow(30_000, transcribing)
  const sheetTimeError = useMemo(() => {
    if (!transcribing) return ''
    const when = fromLocalInput(sheetTime)
    if (!when) return 'Enter the date and time written on the sheet'
    if (when.getTime() > now + 60_000) return 'The time on the sheet cannot be in the future'
    return ''
  }, [transcribing, sheetTime, now])

  const [preview, setPreview] = useState<{ news2: News2; schedule: ObsSchedule } | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [confirmDiscard, setConfirmDiscard] = useState(false)

  // Anything touched, for the draft and the discard question.
  const hasAnyValue = useMemo(
    () => FIELDS.some(f => values[f.key].trim() !== '') || consciousness !== null || oxygen !== null
      || illegible.size > 0,
    [values, consciousness, oxygen, illegible],
  )
  // A reading to record. "Not recorded on the sheet" is a choice, not a reading.
  const hasReading = useMemo(
    () => FIELDS.some(f => values[f.key].trim() !== '') || illegible.size > 0
      || (consciousness !== null && consciousness !== NOT_RECORDED)
      || (oxygen !== null && oxygen !== NOT_RECORDED),
    [values, consciousness, oxygen, illegible],
  )
  // A nurse at the bedside must choose both. The clerk may leave either empty.
  const needsConsciousness = !transcribing && consciousness === null && !illegible.has('consciousness')
  const needsOxygen = !transcribing && oxygen === null && !illegible.has('on_oxygen')
  const choiceMissing = needsConsciousness || needsOxygen

  /** Per-field range validation, so a fat-fingered keypad is caught before save. */
  const fieldErrors = useMemo(() => {
    const errs: Partial<Record<FieldKey, string>> = {}
    for (const f of FIELDS) {
      const raw = values[f.key].trim()
      if (raw === '') continue
      const n = Number(raw)
      if (!Number.isFinite(n)) errs[f.key] = 'Numbers only'
      else if (n < f.min || n > f.max) errs[f.key] = `${f.min}–${f.max} ${f.unit}`
    }
    return errs
  }, [values])

  const hasFieldErrors = Object.keys(fieldErrors).length > 0 || sheetTimeError !== ''

  const buildPayload = useCallback((): ObservationInput => {
    // An empty box, or "Not recorded on the sheet", is sent as null: stored
    // empty, never as alert or on air.
    const payload: ObservationInput = {
      consciousness: consciousness === null || consciousness === NOT_RECORDED ? null : consciousness,
      on_oxygen: oxygen === 'oxygen' ? true : oxygen === 'air' ? false : null,
    }
    for (const f of FIELDS) {
      const raw = values[f.key].trim()
      payload[f.key] = raw === '' || illegible.has(f.key) ? null : Number(raw)
    }
    if (illegible.size > 0) payload.illegible = Array.from(illegible)
    return payload
  }, [values, consciousness, oxygen, illegible])

  /** Tick or untick "Illegible" for one reading. A ticked reading is blanked,
   *  so a half-read number can never travel with the mark. */
  const toggleIllegible = (key: IllegibleField) => {
    setIllegible(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
    if ((FIELDS as { key: string }[]).some(f => f.key === key)) {
      setValues(prev => ({ ...prev, [key]: '' }))
    }
    setShowRestoredNote(false)
  }

  // ── Draft persistence: mirror every change so an interruption cannot lose it.
  //    Stamped with the owner so only this nurse can ever read it back.
  useEffect(() => {
    if (!hasAnyValue) {
      localStorage.removeItem(obsDraftKey(patient.patient_id))
      return
    }
    try {
      const draft: Draft = {
        values, consciousness, oxygen, choices: 2, savedAt: Date.now(), ownerId: ownerId ?? undefined,
        illegible: Array.from(illegible),
        sheetTime: transcribing ? sheetTime : undefined,
      }
      localStorage.setItem(obsDraftKey(patient.patient_id), JSON.stringify(draft))
    } catch {
      // A full/blocked localStorage must never break charting — the in-memory
      // form is still authoritative for this session.
    }
  }, [values, consciousness, oxygen, illegible, sheetTime, transcribing, hasAnyValue,
      patient.patient_id, ownerId])

  // ── Live NEWS2. Debounced, and every in-flight request is aborted when a
  //    newer one starts so a slow response can never overwrite a fresher score.
  // No preview request at all when no score is shown: the clerk holds no
  // vitals.read, and with scoring off the route answers 404.
  const scoreable = hasReading && !hasFieldErrors && showScore
  // Whether a stale score is still in state is irrelevant — an unscoreable form
  // shows no score. Deriving that (rather than clearing state from an effect)
  // means there is no render in which an old NEWS2 sits above edited values.
  const shownPreview = scoreable ? preview : null

  useEffect(() => {
    if (!scoreable) return
    const controller = new AbortController()
    const timer = setTimeout(() => {
      setPreviewing(true)
      previewNews2(patient.patient_id, buildPayload(), controller.signal)
        .then(res => { setPreview({ news2: res.news2, schedule: res.schedule }); setPreviewing(false) })
        .catch(err => {
          if (err?.name === 'AbortError') return
          // A failed preview is not a failed save — say so, don't show a score.
          setPreview(null)
          setPreviewing(false)
        })
    }, 250)
    return () => { controller.abort(); clearTimeout(timer) }
  }, [buildPayload, scoreable, patient.patient_id])

  const requestClose = useCallback(() => {
    if (saving) return
    if (hasAnyValue) { setConfirmDiscard(true); return }
    onClose()
  }, [saving, hasAnyValue, onClose])

  const modalRef = useFocusTrap<HTMLDivElement>(requestClose)

  // The observation time for THIS set of obs, stamped once and then frozen.
  //
  // The server's replay guard is exact equality on (patient, source, taken_at).
  // Stamping inside save() — as this did — gave two taps 6 ms apart two
  // different ISO timestamps, so the guard could never fire and a double-tap
  // charted the observation set twice. One stamp per sheet is what actually
  // makes the guard reachable. It is also the clinically honest value: fixing a
  // typo and re-saving does not change when the reading was taken.
  const takenAtRef = useRef<string | null>(null)
  // Synchronous in-flight latch. `saving` is state, so both taps of a
  // double-tap run against the render where it is still false and both get
  // through; a ref is updated before either can read it.
  const inFlightRef = useRef(false)

  const save = async () => {
    if (inFlightRef.current) return
    if (hasFieldErrors || !hasReading || choiceMissing || saving) return
    inFlightRef.current = true
    setSaving(true)
    setError('')
    if (takenAtRef.current === null) takenAtRef.current = new Date().toISOString()
    // The clerk's set is timed by the sheet, not by the keyboard. Same value on
    // every tap, so the server's replay guard still recognises a double tap.
    const sheetWhen = transcribing ? fromLocalInput(sheetTime) : null
    const takenAt = sheetWhen ? sheetWhen.toISOString() : takenAtRef.current
    try {
      // Offline is a REFUSAL, not a delay. An observation set is the input to a
      // NEWS2 escalation decision and the nurse must see the resulting score
      // before she walks away, so there is nothing useful to do with these
      // numbers without a server. Thrown here (inside the try) so it lands in
      // the same error line every other failure uses, and so the latch below is
      // released and she can simply tap again once she is back in range.
      assertOnlineForWrite('record these observations')
      const res = await recordObservations(patient.patient_id, {
        ...buildPayload(),
        // Stamped on the client, not the server: this is when the reading was
        // taken, and it is what the server dedupes a replay on.
        taken_at: takenAt,
      })
      // Only now is the draft safe to drop. The latch is deliberately NOT
      // released on success — the sheet is closing, and a late second tap must
      // not start another write.
      localStorage.removeItem(obsDraftKey(patient.patient_id))
      const recordedNews2 = showScore ? res.news2 ?? null : null
      const recordedSchedule = showScore ? res.schedule ?? null : null
      // The server's word on the set (a copy too old to raise an alert), only
      // when it gave one.
      if (res.message) onRecorded(recordedNews2, recordedSchedule, res.message)
      else onRecorded(recordedNews2, recordedSchedule)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not record these observations')
      inFlightRef.current = false
      setSaving(false)
    }
  }

  const setField = (key: FieldKey, raw: string) => {
    // Allow only digits and a single decimal point — a tablet number pad can
    // still emit stray characters, and silently coercing them hides typos.
    if (raw !== '' && !/^\d*\.?\d*$/.test(raw)) return
    setValues(prev => ({ ...prev, [key]: raw }))
    setShowRestoredNote(false)
  }

  const pointsFor = (params: string[]): number | null => {
    if (!params.length || !shownPreview) return null
    const hit = shownPreview.news2.breakdown.find(b => params.includes(b.parameter))
    return hit ? hit.points : null
  }

  const severeAllergies = (patient.allergies ?? []).filter(a => a.severity === 'severe')

  /** The "Illegible" tick for one reading, shown on the clerk's copy only. */
  const illegibleTick = (key: IllegibleField, label: string) => transcribing ? (
    <label className="obs-illegible">
      <input
        type="checkbox"
        checked={illegible.has(key)}
        onChange={() => toggleIllegible(key)}
        disabled={saving}
        aria-label={`${label} illegible on the sheet`}
      />
      Illegible
    </label>
  ) : null

  return createPortal(
    <div className="nurse-sheet-backdrop" onClick={requestClose}>
      <div
        className="nurse-sheet glass-strong"
        ref={modalRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="obs-sheet-title"
        onClick={e => e.stopPropagation()}
      >
        <header className="nurse-sheet__header">
          <div>
            <h2 className="nurse-sheet__title" id="obs-sheet-title">
              {transcribing ? 'Copy observations from the sheet' : 'Record observations'}
            </h2>
            <p className="nurse-sheet__subject">
              <strong>{patient.name}</strong>
              {patient.bed ? <> · Bed {patient.bed}</> : null}
              {patient.mrn ? <> · {patient.mrn}</> : null}
            </p>
          </div>
          <button
            type="button"
            className="nurse-sheet__close"
            onClick={requestClose}
            aria-label="Close observation entry"
            disabled={saving}
          >
            &times;
          </button>
        </header>

        <div className="nurse-sheet__body">
          {/* Told at the top of the sheet, not on the submit that follows six
              typed values. The refusal in save() is the guarantee; this is the
              courtesy — a nurse should learn the round cannot be charted before
              she does the work, not after. */}
          {offline && <OfflineBanner snapshot={offline} what="patient details" />}

          {showRestoredNote && restoredAt && (
            <div className="nurse-note nurse-note--info" role="status">
              Unsaved observations from{' '}
              {new Date(restoredAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}{' '}
              were restored. Check them before saving.
            </div>
          )}

          {severeAllergies.length > 0 && (
            <div className="nurse-note nurse-note--danger" role="alert">
              Severe allergy: {severeAllergies.map(a => a.substance).join(', ')}
            </div>
          )}

          {transcribing && (
            <div className={`obs-field obs-sheet-time ${sheetTimeError ? 'obs-field--error' : ''}`}>
              <label className="obs-field__label" htmlFor="obs-sheet-time">
                Time on the sheet
              </label>
              <input
                id="obs-sheet-time"
                className="obs-field__input"
                type="datetime-local"
                required
                max={toLocalInput(new Date(now))}
                value={sheetTime}
                onChange={e => { setSheetTime(e.target.value); setShowRestoredNote(false) }}
                disabled={saving}
                aria-invalid={!!sheetTimeError}
                aria-describedby={sheetTimeError ? 'obs-sheet-time-err' : undefined}
              />
              {sheetTimeError && (
                <span className="obs-field__error" id="obs-sheet-time-err">{sheetTimeError}</span>
              )}
            </div>
          )}

          {showScore && patient.on_scale_2 && (
            <div className="nurse-note nurse-note--warn" role="note">
              SpO₂ scored on <strong>Scale 2</strong> (target 88–92%) for this patient.
            </div>
          )}

          {error && <div className="nurse-note nurse-note--danger" role="alert">{error}</div>}

          <div className="obs-grid">
            {FIELDS.map((f, index) => {
              const points = pointsFor(f.scoreParams)
              const err = fieldErrors[f.key]
              return (
                <div className={`obs-field ${err ? 'obs-field--error' : ''}`} key={f.key}>
                  <label className="obs-field__label" htmlFor={`obs-${f.key}`}>
                    {f.label}
                    <span className="obs-field__unit">{f.unit}</span>
                  </label>
                  <div className="obs-field__row">
                    <input
                      id={`obs-${f.key}`}
                      className="obs-field__input"
                      type="text"
                      inputMode="decimal"
                      autoComplete="off"
                      spellCheck={false}
                      // An <input>'s intrinsic width comes from `size` (20 chars
                      // by default). At this font that is ~400px, which forces
                      // the grid tracks wider than the sheet. Vitals are 1-5
                      // characters; the flex/grid rules do the real sizing.
                      size={4}
                      step={f.step}
                      placeholder="—"
                      value={values[f.key]}
                      onChange={e => setField(f.key, e.target.value)}
                      disabled={saving || illegible.has(f.key)}
                      autoFocus={index === 0}
                      aria-describedby={err ? `obs-${f.key}-err` : undefined}
                      aria-invalid={!!err}
                    />
                    {points !== null && (
                      <span
                        className={`obs-field__points obs-field__points--${points >= 3 ? 'danger' : points >= 1 ? 'warn' : 'ok'}`}
                        title={`Contributes ${points} to NEWS2`}
                      >
                        +{points}
                      </span>
                    )}
                  </div>
                  {err && <span className="obs-field__error" id={`obs-${f.key}-err`}>{err}</span>}
                  {illegibleTick(f.key, f.label)}
                </div>
              )
            })}
          </div>

          <fieldset className="obs-segment">
            <legend className="obs-segment__legend">
              Supplemental oxygen
              {needsOxygen && <span className="obs-segment__hint">Choose one</span>}
            </legend>
            <div className="obs-segment__options" role="group" aria-label="Supplemental oxygen">
              {[
                ...OXYGEN,
                ...(transcribing ? [{ value: NOT_RECORDED as OxygenChoice, label: 'Not recorded on the sheet' }] : []),
              ].map(opt => (
                <button
                  key={opt.value}
                  type="button"
                  className={`obs-chip ${oxygen === opt.value ? 'obs-chip--active' : ''}`}
                  aria-pressed={oxygen === opt.value}
                  onClick={() => { setOxygen(opt.value); setShowRestoredNote(false) }}
                  disabled={saving || illegible.has('on_oxygen')}
                >
                  {opt.label}
                </button>
              ))}
            </div>
            {illegibleTick('on_oxygen', 'Supplemental oxygen')}
          </fieldset>

          <fieldset className="obs-segment">
            <legend className="obs-segment__legend">
              Consciousness (ACVPU)
              {(() => {
                const p = pointsFor(['Consciousness (ACVPU)'])
                return p ? <span className="obs-field__points obs-field__points--danger">+{p}</span> : null
              })()}
              {needsConsciousness && <span className="obs-segment__hint">Choose one</span>}
            </legend>
            <div className="obs-segment__options" role="group" aria-label="Consciousness (ACVPU)">
              {ACVPU.map(level => (
                <button
                  key={level.value}
                  type="button"
                  className={`obs-chip ${consciousness === level.value ? 'obs-chip--active' : ''}`}
                  aria-pressed={consciousness === level.value}
                  onClick={() => { setConsciousness(level.value); setShowRestoredNote(false) }}
                  disabled={saving || illegible.has('consciousness')}
                >
                  <span className="obs-chip__key" aria-hidden="true">{level.hint}</span>
                  {level.label}
                </button>
              ))}
              {transcribing && (
                <button
                  type="button"
                  className={`obs-chip ${consciousness === NOT_RECORDED ? 'obs-chip--active' : ''}`}
                  aria-pressed={consciousness === NOT_RECORDED}
                  onClick={() => { setConsciousness(NOT_RECORDED); setShowRestoredNote(false) }}
                  disabled={saving || illegible.has('consciousness')}
                >
                  Not recorded on the sheet
                </button>
              )}
            </div>
            {illegibleTick('consciousness', 'Consciousness')}
          </fieldset>

          {!showScore && (
            <p className="obs-score__caveat" role="note">
              {transcribing
                ? 'Copy each reading exactly as written. Tick Illegible for any you cannot read. No score is shown.'
                : 'Scoring is switched off on this computer. No score is shown.'}
            </p>
          )}

          {/* The score this set produces — the reason the obs were taken. */}
          {showScore && <section className="obs-score" aria-live="polite">
            {shownPreview ? (
              <>
                <div className="obs-score__head">
                  <News2Badge news2={shownPreview.news2} size="lg" />
                  <div className="obs-score__meta">
                    <span className="obs-score__band">{shownPreview.schedule.band}</span>
                    <span className="obs-score__response">{shownPreview.schedule.response}</span>
                  </div>
                </div>
                {shownPreview.news2.drivers.length > 0 && (
                  <ul className="obs-score__drivers">
                    {shownPreview.news2.drivers.map(d => <li key={d}>{d}</li>)}
                  </ul>
                )}
                <p className="obs-score__caveat">
                  NEWS2 (RCP 2017), computed from the values above, not yet recorded.
                </p>
              </>
            ) : (
              <p className="obs-score__placeholder">
                {previewing
                  ? 'Scoring…'
                  : hasFieldErrors
                    ? 'Fix the highlighted readings to see the NEWS2.'
                    : 'Enter observations to see the NEWS2 they produce.'}
              </p>
            )}
          </section>}
        </div>

        <footer className="nurse-sheet__actions">
          <button type="button" className="nurse-btn nurse-btn--ghost" onClick={requestClose} disabled={saving}>
            Cancel
          </button>
          <button
            type="button"
            className="nurse-btn nurse-btn--primary"
            onClick={save}
            disabled={saving || !hasReading || hasFieldErrors || choiceMissing}
            title={!hasReading
              ? 'Enter at least one reading'
              : hasFieldErrors
                ? 'Fix the highlighted readings'
                : choiceMissing
                  ? 'Choose the level of consciousness and air or oxygen'
                  : undefined}
          >
            {saving ? 'Recording…' : 'Record observations'}
          </button>
        </footer>

        {confirmDiscard && (
          <div className="nurse-confirm" role="alertdialog" aria-labelledby="obs-discard-title">
            <div className="nurse-confirm__box">
              <h3 className="nurse-confirm__title" id="obs-discard-title">Discard these observations?</h3>
              <p className="nurse-confirm__text">
                These readings have not been recorded. They are kept as a draft for the rest of
                your session on this device, and cleared when you sign out. Discarding removes
                them now.
              </p>
              <div className="nurse-confirm__actions">
                <button type="button" className="nurse-btn nurse-btn--ghost" onClick={() => setConfirmDiscard(false)}>
                  Keep entering
                </button>
                <button
                  type="button"
                  className="nurse-btn nurse-btn--secondary"
                  onClick={() => { setConfirmDiscard(false); onClose() }}
                >
                  Keep draft &amp; close
                </button>
                <button
                  type="button"
                  className="nurse-btn nurse-btn--danger"
                  onClick={() => {
                    localStorage.removeItem(obsDraftKey(patient.patient_id))
                    setConfirmDiscard(false)
                    onClose()
                  }}
                >
                  Discard
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}
