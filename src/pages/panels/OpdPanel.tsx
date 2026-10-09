/**
 * Orb: Outpatients (the OPD feature tab, opd-8).
 *
 * The consulting doctor's own screen while the server's 'opd' feature is on:
 *
 *   * MY QUEUE. The patient in the room, the waiting line in seeing order
 *     (priority, then token), the patients who did not come when called, and
 *     the clinic's patients no doctor has taken yet. Call next, call, skip,
 *     back in line, take.
 *   * THE RECORD. Opening a patient shows their outpatient record (opd-7):
 *     identity without the date of birth, allergies with their status,
 *     problems, earlier visits, OPD investigations and signed notes, and the
 *     state of this visit's consultation note (opd-10).
 *   * FINISH VISIT. A disposition, and a reason when there is no signed note
 *     (ruling OPD-N8). The front desk's charge rule raises the fee.
 *   * ADD WALK-IN (opd-8). The front desk's Register view
 *     (frontdesk/RegisterPatientView.tsx) on this tab: the patient index
 *     first, the patient confirms who they are (ruling OPD-S1), a new patient
 *     only after a search; the visit lands in this doctor's queue.
 *   * ALLERGY REVIEW (opd-10a, ruling OPD-S7). The record says "Allergies not
 *     yet asked" or "No known allergies, asked by X on date", and the doctor
 *     records the review: the allergies the patient has (charted, which is
 *     what the interlocks screen), no known allergies, or why they could not
 *     be asked. A note is signed only after one.
 *   * THE NOTE (opd-11). On the doctor's own visits the record carries the
 *     consultation editor (opd/ConsultationEditor.tsx): complaint, history,
 *     examination, timed vitals as plain values, diagnoses as free text with
 *     an optional code, plan, advice, follow-up, referral; save, sign, addenda
 *     and the consultation print. Investigations (laboratory tests and imaging
 *     only, no order sets) are ordered on the visit, with their slip. Finish
 *     visit shows what the note says about a follow-up or a referral;
 *     admission from this screen is not offered yet (opd-17a).
 *
 * WHAT THIS SCREEN NEVER DOES, by design (docs/his_design/opd.json):
 *   * It never shows a NEWS2, a band, a colour on a vital or AI advice. OPD
 *     vitals are plain values with their time (rulings OPD-S3, OPD-S6).
 *   * It never publishes a patient into ActiveViewContext or
 *     ActivePatientContext, and the shell hides the floating Sage assistant on
 *     this tab, so no outpatient becomes a Sage view_context patient.
 *   * It never names a clinic to the server: the queue is the server's answer
 *     for the signed-in account.
 *
 * LIVE UPDATES. The server pushes a content-free 'opd:queue_changed'
 * {department} to the clinic's socket room after every queue change; this
 * screen refetches over its own scoped REST read. While the socket is down it
 * polls every ORB_OPD_QUEUE_POLL_SECONDS instead.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { socket } from '../../api/client'
import { formatWait } from '../../api/frontdesk'
import {
  QUEUE_CHANGED_EVENT,
  callOpdVisit,
  completeOpdVisitWithDisposition,
  errorCode,
  fetchConsultation,
  fetchMyQueue,
  fetchOpdRecord,
  getOpdMeta,
  isFeatureOff,
  recallOpdVisit,
  skipOpdVisit,
  takeOpdVisit,
  tokenWasReissued,
  type CodeLabel,
  type ConsultationView,
  type MyQueue,
  type OpdConsultation,
  type OpdQueueVisit,
  type OpdRecord,
  type OpdVitals,
} from '../../api/opd'
import { useFocusTrap } from '../../hooks/useFocusTrap'
import { useVisiblePolling } from '../../hooks/useVisiblePolling'
import { formatClinicalDateKey, formatClinicalDateTime } from '../../lib/datetime'
import { assessmentOf, reviewStateOf, type AllergyAssessment } from '../../api/opdAllergyReview'
import RegisterPatientView from './frontdesk/RegisterPatientView'
import AllergyReviewDialog from './opd/AllergyReviewDialog'
import ConsultationEditor from './opd/ConsultationEditor'
import { opdVitalsLine } from './opd/opdVitals'
import './OpdPanel.css'

/** Used until /api/opd/meta answers; the server's list wins once it does. */
const FALLBACK_DISPOSITIONS: CodeLabel[] = [
  { code: 'home', label: 'Home' },
  { code: 'follow_up', label: 'Follow-up visit' },
  { code: 'referred_internal', label: 'Referred to another clinic' },
  { code: 'referred_external', label: 'Referred to another hospital' },
]

const NUDGE_DEBOUNCE_MS = 400

function errorText(err: unknown): string {
  if (err instanceof Error && err.message) return err.message
  return 'Something went wrong. Nothing was changed.'
}

function sexAge(v: { patient_age: number | null; patient_sex: string | null }): string {
  return [v.patient_age != null ? `${v.patient_age} y` : null, v.patient_sex]
    .filter(Boolean).join(', ')
}

/** What the doctor is told after Take, built from the visit the server
 *  RETURNED, never the row as it was before: the take issues a new token
 *  when the doctor's own line already holds the patient's number, and the
 *  slip in the patient's hand then shows a number that is someone else's. */
function takenNotice(updated: OpdQueueVisit): string {
  if (tokenWasReissued(updated)) {
    return `Token changed from ${updated.previous_token_number} to ${updated.token_number}. `
      + `Tell the patient: token ${updated.previous_token_number} in your queue is another patient.`
  }
  return `Token ${updated.token_number} is now in your queue.`
}

// ── One row of the queue ─────────────────────────────────────────────────────

type RowAction = { label: string; run: () => Promise<unknown>; primary?: boolean }

function QueueRow({
  visit,
  busy,
  selected,
  onOpen,
  actions,
}: {
  visit: OpdQueueVisit
  busy: boolean
  selected: boolean
  onOpen: () => void
  actions: RowAction[]
}) {
  return (
    <li className={`opd-row ${selected ? 'opd-row--selected' : ''}`}>
      <button
        type="button"
        className="opd-row__open"
        onClick={onOpen}
        aria-pressed={selected}
        aria-label={`Open the record of ${visit.patient_name ?? visit.patient_id}, token ${visit.token_number}`
          + (tokenWasReissued(visit) ? `, was token ${visit.previous_token_number}` : '')}
      >
        <span className="opd-token" aria-hidden="true">{visit.token_number}</span>
        <span className="opd-row__who">
          <span className="opd-row__name">{visit.patient_name ?? visit.patient_id}</span>
          <span className="opd-row__meta">
            {[visit.patient_mrn, sexAge(visit)].filter(Boolean).join(' · ')}
          </span>
          {visit.chief_complaint && (
            <span className="opd-row__complaint">{visit.chief_complaint}</span>
          )}
        </span>
        <span className="opd-row__side">
          {tokenWasReissued(visit) && (
            <span className="opd-token-was">Slip says token {visit.previous_token_number}</span>
          )}
          {visit.priority !== 'normal' && (
            <span className={`opd-priority opd-priority--${visit.priority === 'emergency' ? 'emergency' : 'other'}`}
              title={visit.priority_reason ?? undefined}>
              {visit.priority_label}
            </span>
          )}
          {visit.room && <span className="opd-room">Room {visit.room}</span>}
          <span className="opd-wait">{formatWait(visit.waiting_minutes)}</span>
        </span>
      </button>
      {actions.length > 0 && (
        <div className="opd-row__actions">
          {actions.map(a => (
            <button
              key={a.label}
              type="button"
              className={`opd-btn ${a.primary ? 'opd-btn--primary' : 'opd-btn--quiet'}`}
              disabled={busy}
              onClick={() => { void a.run() }}
            >
              {a.label}
            </button>
          ))}
        </div>
      )}
    </li>
  )
}

function QueueSection({
  title,
  empty,
  visits,
  children,
}: {
  title: string
  empty: string
  visits: OpdQueueVisit[]
  children: (v: OpdQueueVisit) => React.ReactNode
}) {
  return (
    <section className="opd-card" aria-label={title}>
      <h3 className="opd-card__title">
        {title} <span className="opd-count">{visits.length}</span>
      </h3>
      {visits.length === 0
        ? <p className="opd-empty">{empty}</p>
        : <ul className="opd-list">{visits.map(v => children(v))}</ul>}
    </section>
  )
}

// ── The record of the open patient ───────────────────────────────────────────

function vitalsLine(v: OpdVitals): string {
  return opdVitalsLine(v.values)
}

function NoteState({ view }: { view: ConsultationView | null }) {
  if (!view) return <p className="opd-empty">Loading the consultation note…</p>
  const note = view.consultation
  return (
    <div className="opd-note">
      <h4 className="opd-subhead">This visit&rsquo;s note</h4>
      {!note && <p className="opd-empty">No note has been started for this visit.</p>}
      {note && note.status === 'draft' && (
        <p className="opd-line">Draft, version {note.row_version}, last saved {formatClinicalDateTime(note.updated_at)}.</p>
      )}
      {note && note.status === 'signed' && (
        <p className="opd-line">
          Signed by {note.signed_by_name ?? note.signed_by} on {formatClinicalDateTime(note.signed_at)}
          {note.addenda && note.addenda.length > 0 ? `, with ${note.addenda.length} addenda` : ''}.
        </p>
      )}
      {note && note.diagnoses.length > 0 && (
        <p className="opd-line">Diagnoses: {note.diagnoses.map(d => d.text).join('; ')}</p>
      )}
      {note && note.plan && <p className="opd-line">Plan: {note.plan}</p>}
      {note?.vitals && (
        <p className="opd-line">
          Measured {formatClinicalDateTime(note.vitals.measured_at)}
          {note.vitals.measured_by_name ? ` by ${note.vitals.measured_by_name}` : ''}: {vitalsLine(note.vitals)}
        </p>
      )}
      {!view.allergy_review.can_sign && (
        <p className="opd-hint">
          A note can be signed once the patient&rsquo;s allergies are recorded in an allergy
          review: their allergies, no known allergies, or why they could not be asked. Until then
          the visit can be finished with a reason.
        </p>
      )}
      <ReviewBasis view={view} />
    </div>
  )
}

/** Which allergy review lets this note be signed (opd-10a). */
function ReviewBasis({ view }: { view: ConsultationView }) {
  const review = reviewStateOf(view)
  if (!review?.can_sign || !review.assessment) return null
  return (
    <p className="opd-hint">
      {review.basis === 'this_visit'
        ? 'Allergy review recorded on this visit.'
        : `Allergy review from an earlier visit, ${formatClinicalDateTime(review.assessment.assessed_at)}.`}
    </p>
  )
}

/** What the patient said when last asked about allergies (opd-10a). */
function AllergyReviewLine({ assessment, charted }: {
  assessment: AllergyAssessment | null
  charted: number
}) {
  if (!assessment) {
    /* Allergies charted elsewhere (an admission, for example) with no review:
       the list below is real, so the line does not say "not yet asked". */
    return charted > 0 ? (
      <p className="opd-line">
        <strong>No allergy review on record.</strong> The allergies below were charted without
        one. Confirm them with the patient in a review.
      </p>
    ) : (
      <p className="opd-line">
        <strong>Allergies not yet asked.</strong> No allergy review is on record for this patient.
      </p>
    )
  }
  const who = `${assessment.assessed_by_name ?? assessment.assessed_by} on `
    + formatClinicalDateTime(assessment.assessed_at)
  if (assessment.status === 'none_known') {
    return charted > 0
      ? <p className="opd-line">The last review, by {who}, said no known allergies; allergies have been charted since. Review again.</p>
      : <p className="opd-line">No known allergies, asked by {who}.</p>
  }
  if (assessment.status === 'unable_to_obtain') {
    return (
      <p className="opd-line">
        Allergies could not be obtained{assessment.reason ? ` (${assessment.reason})` : ''}, recorded
        by {who}. Ask again when you can.
      </p>
    )
  }
  return <p className="opd-line">Allergies reviewed by {who}.</p>
}

function RecordPane({
  visit,
  record,
  note,
  error,
  canFinish,
  onFinish,
  onReview,
  onNoteChanged,
}: {
  visit: OpdQueueVisit
  record: OpdRecord | null
  note: ConsultationView | null
  error: string
  canFinish: boolean
  onFinish: () => void
  /** Record the allergy review (opd-10a); offered on the doctor's own visits. */
  onReview?: () => void
  /** The note editor (opd-11): offered on the doctor's own visits. */
  onNoteChanged?: (view: ConsultationView) => void
}) {
  if (error) return <p className="opd-error" role="alert">{error}</p>
  /* Never draw one patient's record or note under another patient's token. */
  if (!record || record.patient.id !== visit.patient_id) {
    return <p className="opd-empty">Loading the record…</p>
  }
  const visitNote = note && note.visit_id === visit.id ? note : null
  const p = record.patient
  return (
    <div className="opd-record__body">
      <header className="opd-record__head">
        <div>
          <h3 className="opd-record__name">{p.name}</h3>
          <p className="opd-row__meta">
            {[p.uhid && `UHID ${p.uhid}`, p.age != null ? `${p.age} y` : null, p.sex,
              `token ${visit.token_number}`].filter(Boolean).join(' · ')}
          </p>
          {record.access.arm === 'opd_visit' && record.access.scope_until && (
            <p className="opd-hint">
              Open to your clinic until {formatClinicalDateKey(record.access.scope_until)} because of
              this visit.
            </p>
          )}
        </div>
        {canFinish && visitNote && (
          <button type="button" className="opd-btn opd-btn--primary" onClick={onFinish}>
            Finish visit
          </button>
        )}
      </header>

      <section className="opd-block" aria-label="Allergies">
        <h4 className="opd-subhead">Allergies</h4>
        <AllergyReviewLine assessment={assessmentOf(record)} charted={record.allergies.length} />
        {record.allergy_status === 'none_recorded' ? (
          assessmentOf(record) ? null : (
            <p className="opd-line">
              No allergies recorded. This is not the same as no known allergies: nobody may have asked yet.
            </p>
          )
        ) : (
          <ul className="opd-plain-list">
            {record.allergies.map(a => (
              <li key={a.substance}>
                {a.substance}
                {a.reaction ? `: ${a.reaction}` : ''}
                {a.severity && a.severity !== 'unknown' ? ` (${a.severity})` : ''}
              </li>
            ))}
          </ul>
        )}
        {onReview && (
          <div className="opd-row__actions">
            <button type="button" className="opd-btn" onClick={onReview}>
              Record allergy review
            </button>
          </div>
        )}
      </section>

      <section className="opd-block" aria-label="Problems">
        <h4 className="opd-subhead">Problems</h4>
        {record.problems.length === 0
          ? <p className="opd-empty">None recorded.</p>
          : (
            <ul className="opd-plain-list">
              {record.problems.map(pr => (
                <li key={`${pr.problem}-${pr.icd10_code ?? ''}`}>
                  {pr.problem}{pr.icd10_code ? ` (${pr.icd10_code})` : ''}
                </li>
              ))}
            </ul>
          )}
      </section>

      {onNoteChanged && visitNote ? (
        <ConsultationEditor
          key={visit.id}
          visitId={visit.id}
          patientName={p.name}
          view={visitNote}
          onChanged={onNoteChanged}
          onReviewAllergies={onReview}
          visitOpen={visit.status !== 'completed' && visit.status !== 'cancelled'}
        />
      ) : (
        <NoteState view={visitNote} />
      )}

      <section className="opd-block" aria-label="Signed outpatient notes">
        <h4 className="opd-subhead">Signed outpatient notes</h4>
        {record.consultations.length === 0
          ? <p className="opd-empty">None yet.</p>
          : (
            <ul className="opd-plain-list">
              {record.consultations.map(c => (
                <li key={c.id}>
                  {formatClinicalDateKey(c.visit_date)} · {c.department}
                  {c.signed_by_name ? ` · ${c.signed_by_name}` : ''}
                  {c.diagnoses.length > 0 ? `: ${c.diagnoses.join('; ')}` : ''}
                </li>
              ))}
            </ul>
          )}
      </section>

      <section className="opd-block" aria-label="Outpatient visits">
        <h4 className="opd-subhead">Outpatient visits</h4>
        <ul className="opd-plain-list">
          {record.opd_visits.map(v => (
            <li key={v.id}>
              {formatClinicalDateKey(v.visit_date)} · {v.department}
              {v.doctor_name ? ` · ${v.doctor_name}` : ''}
              {v.own_clinic && v.chief_complaint ? `: ${v.chief_complaint}` : ''}
            </li>
          ))}
        </ul>
      </section>

      <section className="opd-block" aria-label="Investigations">
        <h4 className="opd-subhead">Investigations from outpatient visits</h4>
        {record.results.length === 0
          ? <p className="opd-empty">None.</p>
          : (
            <ul className="opd-plain-list">
              {record.results.map(r => (
                <li key={r.id}>
                  {r.description}: {r.result_text ? r.result_text : 'result awaited'}
                </li>
              ))}
            </ul>
          )}
      </section>
    </div>
  )
}

// ── Finish visit ─────────────────────────────────────────────────────────────

/** What the note says about the outcome chosen (opd-11): the follow-up date
 *  or the clinic referred to, as written, or that the note does not say. */
function DispositionNote({ disposition, note }: { disposition: string; note: OpdConsultation | null }) {
  if (disposition === 'follow_up') {
    return note?.follow_up_date ? (
      <p className="opd-hint">
        Follow-up on {formatClinicalDateKey(note.follow_up_date)}, from the note. The front desk books
        the appointment.
      </p>
    ) : (
      <p className="opd-hint">
        The note has no follow-up date. Add one to the note, or tell the front desk when.
      </p>
    )
  }
  if (disposition === 'referred_internal') {
    return note?.referral_department ? (
      <p className="opd-hint">Referred to {note.referral_department}, from the note.</p>
    ) : (
      <p className="opd-hint">The note names no clinic to refer to. Add it to the note.</p>
    )
  }
  return null
}

function FinishVisitDialog({
  visit,
  signed,
  note,
  dispositions,
  onClose,
  onDone,
}: {
  visit: OpdQueueVisit
  signed: boolean
  note: OpdConsultation | null
  dispositions: CodeLabel[]
  onClose: () => void
  onDone: () => void
}) {
  const [disposition, setDisposition] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const close = useCallback(() => { if (!busy) onClose() }, [busy, onClose])
  const ref = useFocusTrap<HTMLDivElement>(close)
  const ready = !busy && disposition !== '' && (signed || reason.trim().length >= 3)

  const submit = async () => {
    if (!ready) return
    setBusy(true)
    setError('')
    try {
      await completeOpdVisitWithDisposition(visit.id, disposition, signed ? undefined : reason.trim())
      onDone()
    } catch (err) {
      setError(errorCode(err) === 'signed_note_required'
        ? 'This visit has no signed note. Say why, then finish it.'
        : errorText(err))
      setBusy(false)
    }
  }

  return createPortal(
    <div className="opd-modal-backdrop" onClick={close}>
      <div
        className="opd-modal"
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-labelledby="opd-finish-title"
        onClick={e => e.stopPropagation()}
      >
        <h2 className="opd-modal__title" id="opd-finish-title">
          Finish the visit of {visit.patient_name ?? visit.patient_id}
        </h2>
        <div className="opd-field">
          <label htmlFor="opd-disposition">Outcome</label>
          <select id="opd-disposition" value={disposition} disabled={busy}
            onChange={e => setDisposition(e.target.value)}>
            <option value="">Choose the outcome…</option>
            {dispositions.map(d => <option key={d.code} value={d.code}>{d.label}</option>)}
            {/* Admitting from outpatients is its own step (opd-17a), not built yet. */}
            <option value="advise_admission" disabled>Advise admission (not available here yet)</option>
          </select>
        </div>
        <DispositionNote disposition={disposition} note={note} />
        {disposition === '' && (
          <p className="opd-hint">
            To admit the patient, send them to the admission desk with your note.
          </p>
        )}
        {!signed && (
          <div className="opd-field">
            <label htmlFor="opd-no-note-reason">Why is there no signed note?</label>
            <textarea id="opd-no-note-reason" rows={2} value={reason} disabled={busy}
              placeholder="For example, the note is written on paper"
              onChange={e => setReason(e.target.value)} />
          </div>
        )}
        <p className="opd-hint">Finishing raises the consultation charge for the billing counter.</p>
        {error && <p className="opd-error" role="alert">{error}</p>}
        <div className="opd-modal__actions">
          <button type="button" className="opd-btn opd-btn--quiet" onClick={close} disabled={busy}>
            Cancel
          </button>
          <button type="button" className="opd-btn opd-btn--primary" onClick={submit} disabled={!ready}>
            {busy ? 'Finishing…' : 'Finish visit'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

// ── The panel ────────────────────────────────────────────────────────────────

type LoadState = 'loading' | 'ready' | 'off' | 'error'

export default function OpdPanel() {
  const [queue, setQueue] = useState<MyQueue | null>(null)
  const [state, setState] = useState<LoadState>('loading')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busyId, setBusyId] = useState('')
  const [openVisit, setOpenVisit] = useState<OpdQueueVisit | null>(null)
  const [record, setRecord] = useState<OpdRecord | null>(null)
  const [note, setNote] = useState<ConsultationView | null>(null)
  const [recordError, setRecordError] = useState('')
  const [finishing, setFinishing] = useState(false)
  const [reviewing, setReviewing] = useState(false)
  const [walkIn, setWalkIn] = useState(false)
  const [dispositions, setDispositions] = useState<CodeLabel[]>(FALLBACK_DISPOSITIONS)
  const [connected, setConnected] = useState<boolean>(() => socket?.connected === true)
  const mounted = useRef(true)
  /* Every record open (and every close) takes the next number. A response is
     used only while its number is still the latest, so a slow answer for an
     earlier patient can never land under a later patient's token. */
  const openSeq = useRef(0)

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const load = useCallback(async () => {
    try {
      const q = await fetchMyQueue()
      if (!mounted.current) return
      setQueue(q)
      setState('ready')
      setError('')
    } catch (err) {
      if (!mounted.current) return
      if (isFeatureOff(err)) {
        setState('off')
        return
      }
      setError(errorText(err))
      setState(prev => (prev === 'ready' ? 'ready' : 'error'))
    }
  }, [])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger: the setState is the loader's own result, not state derived from props.
  useEffect(() => { void load() }, [load])

  useEffect(() => {
    let alive = true
    getOpdMeta()
      .then(meta => {
        if (alive && Array.isArray(meta.dispositions) && meta.dispositions.length) {
          setDispositions(meta.dispositions)
        }
      })
      .catch(() => { /* the fallback list stands; the server still validates */ })
    return () => { alive = false }
  }, [])

  /* The live nudge: refetch (debounced) on 'opd:queue_changed'. The payload is
     the clinic and nothing else, so it is never read. Track the socket so the
     fallback poll runs only while it is down. */
  useEffect(() => {
    if (!socket) return
    let timer: ReturnType<typeof setTimeout> | undefined
    const onNudge = () => {
      if (timer) return
      timer = setTimeout(() => { timer = undefined; void load() }, NUDGE_DEBOUNCE_MS)
    }
    const onConnect = () => setConnected(true)
    const onDisconnect = () => setConnected(false)
    socket.on(QUEUE_CHANGED_EVENT, onNudge)
    socket.on('connect', onConnect)
    socket.on('disconnect', onDisconnect)
    return () => {
      socket.off(QUEUE_CHANGED_EVENT, onNudge)
      socket.off('connect', onConnect)
      socket.off('disconnect', onDisconnect)
      if (timer) clearTimeout(timer)
    }
  }, [load])

  const pollMs = Math.max(5, queue?.poll_seconds ?? 30) * 1000
  useVisiblePolling(() => { void load() }, pollMs, state !== 'off' && !connected)

  const openRecord = useCallback(async (visit: OpdQueueVisit) => {
    const seq = ++openSeq.current
    setFinishing(false)
    setReviewing(false)
    setOpenVisit(visit)
    setRecord(null)
    setNote(null)
    setRecordError('')
    try {
      const [r, n] = await Promise.all([fetchOpdRecord(visit.patient_id), fetchConsultation(visit.id)])
      if (!mounted.current || seq !== openSeq.current) return
      if (r?.patient?.id !== visit.patient_id || n?.visit_id !== visit.id) {
        setRecordError('The record that came back is not this patient’s. Open the patient again.')
        return
      }
      setRecord(r)
      setNote(n)
    } catch (err) {
      if (!mounted.current || seq !== openSeq.current) return
      setRecordError(errorCode(err) === null && (err as { status?: number })?.status === 404
        ? 'This record is not open to your clinic.'
        : errorText(err))
    }
  }, [])

  const closeRecord = useCallback(() => {
    openSeq.current += 1
    setFinishing(false)
    setReviewing(false)
    setOpenVisit(null)
    setRecord(null)
    setNote(null)
    setRecordError('')
  }, [])

  /* Read the open record and note again without closing them (after an
     allergy review), under the same rule as openRecord: an answer is used
     only while its patient is still the one open. */
  const refreshRecord = useCallback(async (visit: OpdQueueVisit) => {
    const seq = ++openSeq.current
    try {
      const [r, n] = await Promise.all([fetchOpdRecord(visit.patient_id), fetchConsultation(visit.id)])
      if (!mounted.current || seq !== openSeq.current) return
      if (r?.patient?.id !== visit.patient_id || n?.visit_id !== visit.id) {
        setRecordError('The record that came back is not this patient’s. Open the patient again.')
        return
      }
      setRecord(r)
      setNote(n)
    } catch (err) {
      if (!mounted.current || seq !== openSeq.current) return
      setNotice('')
      setError(errorText(err))
    }
  }, [])

  /* The note editor's answer (opd-11): kept only while it is still the open
     visit's note. */
  const noteChanged = useCallback((view: ConsultationView) => {
    setNote(prev => (prev && prev.visit_id === view.visit_id ? view : prev))
  }, [])

  /* `done` builds the notice from the visit the server returned (its token
     may differ from the row's: see takenNotice). */
  const act = useCallback(async (
    visit: OpdQueueVisit,
    fn: () => Promise<OpdQueueVisit>,
    done?: (updated: OpdQueueVisit) => string,
  ) => {
    setBusyId(visit.id)
    setError('')
    setNotice('')
    try {
      const updated = await fn()
      if (done) setNotice(done(updated))
      /* Compare against the visit open NOW, not the one open when the action
         started: the doctor may have opened another patient meanwhile. */
      setOpenVisit(prev => (prev && prev.id === updated.id ? updated : prev))
      await load()
    } catch (err) {
      setError(errorText(err))
    } finally {
      if (mounted.current) setBusyId('')
    }
  }, [load])

  /* The walk-in (opd-8): the Register view's callbacks, stable so it loads its
     settings once. It never names a clinic; the server derives it. */
  const noOtherView = useCallback(() => {}, [])
  const walkInOff = useCallback(() => setWalkIn(false), [])
  const walkInRegistered = useCallback((visit: { token_number: number; patient_name: string | null }) => {
    setNotice(`Token ${visit.token_number} registered for ${visit.patient_name ?? 'the patient'}.`)
    void load()
  }, [load])

  if (state === 'loading') return <div className="orb-panel-loading">Loading your outpatient queue…</div>
  if (state === 'off') {
    return (
      <div className="orb-panel-loading" role="status">
        Outpatients is switched off on this site. An administrator can switch it on.
      </div>
    )
  }
  if (state === 'error' || !queue) {
    return (
      <div className="orb-panel-error" role="alert">
        <p>{error || 'The queue could not be loaded.'}</p>
        <button type="button" onClick={() => { void load() }}>Try again</button>
      </div>
    )
  }

  const nextUp = queue.waiting[0] ?? null
  const mineOpen = openVisit && [...queue.current, ...queue.waiting, ...queue.skipped]
    .some(v => v.id === openVisit.id)
  /* Only the open visit's own note may decide whether a reason is needed. */
  const openNote = openVisit && note && note.visit_id === openVisit.id ? note : null
  const row = (v: OpdQueueVisit, actions: RowAction[]) => (
    <QueueRow
      key={v.id}
      visit={v}
      busy={busyId === v.id}
      selected={openVisit?.id === v.id}
      onOpen={() => { void openRecord(v) }}
      actions={actions}
    />
  )

  return (
    <div className="orb-panel opd-panel">
      <header className="opd-header">
        <div>
          <h2 className="opd-title">Outpatients</h2>
          <p className="opd-subtitle">
            Your queue{queue.department ? ` in ${queue.department}` : ''} for {formatClinicalDateKey(queue.visit_date)}
            {queue.completed_count ? `. Seen today: ${queue.completed_count}` : ''}
          </p>
        </div>
        <div className="opd-header__actions">
          <button
            type="button"
            className="opd-btn opd-btn--primary"
            disabled={!nextUp || busyId !== ''}
            onClick={() => {
              if (nextUp) {
                void act(nextUp, () => callOpdVisit(nextUp.id))
                void openRecord(nextUp)
              }
            }}
          >
            {nextUp ? `Call next (token ${nextUp.token_number})` : 'No one waiting'}
          </button>
          <button type="button" className="opd-btn opd-btn--quiet" onClick={() => { void load() }}>
            Refresh
          </button>
          <button
            type="button"
            className="opd-btn opd-btn--quiet"
            aria-expanded={walkIn}
            onClick={() => setWalkIn(open => !open)}
          >
            {walkIn ? 'Close walk-in' : 'Add walk-in'}
          </button>
        </div>
      </header>

      {error && <p className="opd-error" role="alert">{error}</p>}
      {notice && <p className="opd-notice" role="status">{notice}</p>}

      {walkIn && (
        <section className="opd-card" aria-label="Add a walk-in">
          <RegisterPatientView
            scopedDepartment={queue.department}
            openView={noOtherView}
            onFeatureOff={walkInOff}
            context="opd"
            defaultDoctorId={queue.doctor_id}
            onVisitRegistered={walkInRegistered}
          />
        </section>
      )}

      <div className="opd-layout">
        <div className="opd-queue">
          <QueueSection title="With you now" empty="No one is in with you." visits={queue.current}>
            {v => row(v, [
              { label: 'Skip', run: () => act(v, () => skipOpdVisit(v.id)) },
            ])}
          </QueueSection>
          <QueueSection title="Waiting" empty="No one is waiting for you." visits={queue.waiting}>
            {v => row(v, [
              { label: 'Call in', primary: true, run: () => act(v, () => callOpdVisit(v.id)) },
              { label: 'Skip', run: () => act(v, () => skipOpdVisit(v.id)) },
            ])}
          </QueueSection>
          <QueueSection title="Skipped" empty="No one has been skipped." visits={queue.skipped}>
            {v => row(v, [
              { label: 'Back in line', run: () => act(v, () => recallOpdVisit(v.id)) },
              { label: 'Call in', run: () => act(v, () => callOpdVisit(v.id)) },
            ])}
          </QueueSection>
          <QueueSection title="Not yet assigned" empty="Every patient in your clinic has a doctor."
            visits={queue.unassigned}>
            {v => row(v, [
              { label: 'Take', run: () => act(v, () => takeOpdVisit(v.id), takenNotice) },
            ])}
          </QueueSection>
        </div>

        <section className="opd-card opd-record" aria-label="Patient record">
          {openVisit
            ? (
              <RecordPane
                visit={openVisit}
                record={record}
                note={note}
                error={recordError}
                canFinish={!!mineOpen}
                onFinish={() => setFinishing(true)}
                onReview={mineOpen ? () => setReviewing(true) : undefined}
                onNoteChanged={mineOpen ? noteChanged : undefined}
              />
            )
            : <p className="opd-empty">Choose a patient to open their outpatient record.</p>}
        </section>
      </div>

      {reviewing && openVisit && record && record.patient.id === openVisit.patient_id && (
        <AllergyReviewDialog
          visitId={openVisit.id}
          patientName={openVisit.patient_name ?? record.patient.name}
          charted={record.allergies.map(a => a.substance)}
          onClose={() => setReviewing(false)}
          onDone={result => {
            setReviewing(false)
            setNotice(`Allergy review saved for ${openVisit.patient_name ?? record.patient.name}: `
              + `${result.assessment.status_label.toLowerCase()}.`)
            /* Read the record and the note again in place, so a note being
               written is not closed (and its unsaved words lost). */
            void refreshRecord(openVisit)
          }}
        />
      )}

      {finishing && openVisit && openNote && (
        <FinishVisitDialog
          visit={openVisit}
          signed={openNote.consultation?.status === 'signed'}
          note={openNote.consultation}
          dispositions={dispositions}
          onClose={() => setFinishing(false)}
          onDone={() => {
            setFinishing(false)
            setNotice(`Visit of ${openVisit.patient_name ?? openVisit.patient_id} finished.`)
            closeRecord()
            void load()
          }}
        />
      )}
    </div>
  )
}
