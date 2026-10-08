/**
 * Orb — Front Desk.
 *
 * Two things a hospital cannot run without and Orb did not have:
 *
 *   1. THE OPD QUEUE. Register an outpatient, issue a token, watch the wait,
 *      call them in, finish the consultation. Finishing raises the consultation
 *      charge through the billing spine; this screen never computes a price.
 *
 *   2. THE TRANSFER. Orb had admit and discharge and nothing between them, so a
 *      patient moving ward left no record at all. A transfer here writes a
 *      movement with a from/to, a timestamp and a reason.
 *
 * ── THE THING THIS SCREEN MUST SAY OUT LOUD ─────────────────────────────────
 * A transfer changes WHO CAN SEE THE PATIENT. Ward scope is derived live from
 * the admission row, so the moment a transfer commits the receiving ward can
 * open the chart and the sending ward cannot. A clerk clicking "Transfer"
 * because a bed is needed is also handing over the record, and the confirm step
 * says that in those words rather than leaving it to be discovered.
 *
 * ── DESIGN RULES OBSERVED ───────────────────────────────────────────────────
 *  * No `opacity` for de-emphasis anywhere (it multiplies down the tree and
 *    fades label and fill by the same factor). Disabled controls use the flat
 *    --btn-disabled-* pair.
 *  * Status colour: fills use --status-x, small text uses --status-x-text, text
 *    ON a fill uses --status-on-fill.
 *  * Modals portal to document.body.
 *  * Money renders with tabular-lining figures, and an unpriced item renders
 *    "Not configured" — never ₹0.00.
 *
 * ── MORE DESK SECTIONS, WITH THE REGISTRATION FEATURE (opd-15) ──────────────
 * While the server's 'registration' feature is on, the desk also offers every
 * section found in ./frontdesk/*View.tsx (clinic timings first; the register,
 * appointments, day summary and display board views arrive the same way), each
 * as its own file under the contract in ./frontdesk/viewRegistry.ts. With the
 * feature off this screen is exactly the OPD queue and Transfers it always was.
 *
 * A desk-only account (the receptionist: registration.* and no chart) sees no
 * Transfers and is not offered Call in or Complete, which the server refuses
 * it; it reads the queue and the roster and marks no-shows.
 *
 * ── THE OPD QUEUE WITH THE 'opd' FEATURE (opd-8) ───────────────────────────
 * When the server marks its queue answer opd_mode, the OPD queue section is
 * ./frontdesk/QueueView.tsx: priority (with its reason), room and skipped, the
 * desk's "Priority and room" and "Left without being seen" (a coded cancel, no
 * charge), and a re-read every ORB_OPD_QUEUE_POLL_SECONDS until live desk
 * nudges land (opd-9b). Calling in needs opd.consult; finishing a visit is on
 * the doctor's Outpatients tab. Without the mark the queue below is unchanged.
 *
 * ── REGISTERING A VISIT WITH THE REGISTRATION FEATURE (opd-6a) ────────────
 * The server then refuses any visit whose patient has not confirmed who they
 * are (ruling OPD-S1), so the old form, which picked from the ward's patient
 * list, is not offered. The OPD queue section points to the Register view
 * (./frontdesk/RegisterPatientView.tsx): the patient index search first, the
 * identity check, a new registration only after a search. With the feature
 * off the form is exactly the one it always was.
 */
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'
import { apiFetch } from '../../api/client'
import { useFocusTrap } from '../../hooks/useFocusTrap'
import { useDataChanged } from '../../hooks/useDataChanged'
import { formatInrOrUnset } from '../../lib/money'
import {
  cancelOpdVisit,
  completeOpdVisit,
  fetchConsultingDoctors,
  fetchOpdQueue,
  fetchPatientMovements,
  fetchTransferDestinations,
  formatWait,
  registerOpdVisit,
  startOpdConsultation,
  transferPatient,
  waitBand,
  type ConsultingDoctor,
  type OpdQueueResponse,
  type OpdVisit,
  type PatientMovement,
  type TransferDestination,
} from '../../api/frontdesk'
import './FrontDeskPanel.css'
import { formatClinicalDateTime } from '../../lib/datetime'
import { OPD_CONSULT, PATIENT_READ_ALL, hasPermission, type Permission } from '../../lib/roles'
import QueueView, { type OpdModeQueue } from './frontdesk/QueueView'
import { currentFeatureFlags, subscribeModuleFlags } from '../../shellConfig'
import {
  discoverFrontDeskViews,
  visibleFrontDeskViews,
  type FrontDeskViewEntry,
} from './frontdesk/viewRegistry'

/** 'opd', 'transfers', or the id of a discovered desk view. */
type View = string

/** Every installed desk view, read once at module load (the glob is compile-time). */
const INSTALLED_DESK_VIEWS: readonly FrontDeskViewEntry[] = discoverFrontDeskViews(
  /* QueueView.tsx is the built-in OPD queue's OPD-mode body (opd-8), not a
     discovered section, so it is left out here and rendered below. */
  import.meta.glob(['./frontdesk/*View.tsx', '!./frontdesk/QueueView.tsx'], { eager: true }),
)

/** The client permission mirror; a permission it does not list reads as not held. */
function roleHolds(permission: string): boolean {
  return hasPermission(permission as Permission)
}

/** The server says the 'opd' feature is on by marking its queue answer. */
function isOpdModeQueue(queue: OpdQueueResponse): queue is OpdQueueResponse & OpdModeQueue {
  return (queue as { opd_mode?: unknown }).opd_mode === true
}

/** A patient on the caller's own ward, from the clinical board. The server
 *  scopes this list; the panel never asks for a department. */
interface WardPatient {
  id: string
  name: string
  mrn: string
  ward: string | null
  bed: string | null
  admission_department?: string | null
}

function errorText(err: unknown): string {
  if (err instanceof Error && err.message) return err.message
  return 'Something went wrong. Nothing was saved.'
}

// ── Small presentational pieces ──────────────────────────────────────────────

function WaitPill({ minutes }: { minutes: number | null }) {
  const band = waitBand(minutes)
  return (
    <span className={`fd-wait fd-wait--${band}`} title="Time since registration">
      {formatWait(minutes)}
    </span>
  )
}

function StatusChip({ status }: { status: OpdVisit['status'] }) {
  const label: Record<OpdVisit['status'], string> = {
    waiting: 'Waiting',
    in_consultation: 'In consultation',
    completed: 'Completed',
    cancelled: 'Cancelled',
  }
  return <span className={`fd-chip fd-chip--${status}`}>{label[status]}</span>
}

function EmptyNote({ children }: { children: React.ReactNode }) {
  return <p className="fd-empty">{children}</p>
}

/** The id of the Register view (./frontdesk/RegisterPatientView.tsx). */
const REGISTER_VIEW_ID = 'register'

/** With the registration feature on, the OPD section's way to register a
 *  visit: the Register view, search first, then the identity check. */
function RegisterVisitPointer({ onOpen }: { onOpen: (() => void) | null }) {
  return (
    <section className="fd-card fd-register-pointer" aria-label="Register a visit">
      <h3 className="fd-card__title">Register a visit</h3>
      {onOpen ? (
        <>
          <p className="fd-hint">
            Find the patient in the index and confirm who they are before the visit is attached
            to their record. A new patient is registered there too, after a search.
          </p>
          <div className="fd-actions">
            <button type="button" className="fd-btn fd-btn--primary" onClick={onOpen}>
              Find or register a patient
            </button>
          </div>
        </>
      ) : (
        <p className="fd-hint">
          A visit is registered from the patient index, after the patient confirms who they are.
          Your account cannot open the index, so a colleague at the desk registers the visit.
        </p>
      )}
    </section>
  )
}

function sameDepartment(a: string | null | undefined, b: string | null | undefined): boolean {
  return (a ?? '').trim().toLowerCase() === (b ?? '').trim().toLowerCase()
}

/** A doctor's name, plus whatever tells two of the same name apart: the
 *  department when theirs differ, otherwise the staff id. */
function doctorLabel(d: ConsultingDoctor, list: ConsultingDoctor[]): string {
  const namesakes = list.filter(o => o.name === d.name)
  if (namesakes.length < 2) return d.name
  const departmentsDiffer = namesakes.some(o => !sameDepartment(o.department, d.department))
  return departmentsDiffer && d.department ? `${d.name} (${d.department})` : `${d.name} (${d.id})`
}

// ── Register-a-visit form ────────────────────────────────────────────────────

function RegisterVisitForm({
  patients,
  doctors,
  clinics,
  scopedDepartment,
  onRegistered,
}: {
  patients: WardPatient[]
  doctors: ConsultingDoctor[]
  /** Departments with clinical cover — the only legal clinics to register into. */
  clinics: string[]
  /** The caller's own department AS THE SERVER DERIVED IT, or null for an
   *  unscoped governance account. Never guessed from the signed-in user object. */
  scopedDepartment: string | null
  onRegistered: () => void
}) {
  const [patientId, setPatientId] = useState('')
  const [doctorId, setDoctorId] = useState('')
  const [complaint, setComplaint] = useState('')
  const [visitType, setVisitType] = useState<'new' | 'follow_up' | 'review'>('new')
  const [department, setDepartment] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  /**
   * WHICH CLINIC — asked only when the server cannot answer it for us.
   *
   * A desk clerk scoped to a department registers into that department and is
   * not asked; the server fills it in and REFUSES a different one (registering
   * into someone else's clinic is the ward boundary going the wrong way).
   *
   * An account with no department is a hospital-wide governance account, and for
   * it the server has no default to fall back on — it answers 422 "department is
   * required for an account with no department on record". The form had no way
   * to say which clinic, so such an account could not register a visit at all.
   * That became reachable the moment ADM001's ward scoping was corrected to
   * NULL, which is why it had never been hit before.
   */
  const mustChooseClinic = scopedDepartment === null

  /* Only the doctors of the clinic this visit is for. The list used to offer
     every doctor in the hospital to a governance account, and the server took
     any user id, so a visit could be booked with another clinic's doctor (or a
     nurse) and printed that name on the token. The server now refuses that; the
     list matches it. */
  const clinic = mustChooseClinic ? department : scopedDepartment
  const clinicDoctors = clinic
    ? doctors.filter(d => sameDepartment(d.department, clinic))
    : []
  // A clinic change can take the chosen doctor out of the list; never send them.
  const chosenDoctor = clinicDoctors.some(d => d.id === doctorId) ? doctorId : ''

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!patientId || busy) return
    if (mustChooseClinic && !department) {
      setError('Choose which clinic this visit is for.')
      return
    }
    setBusy(true)
    setError('')
    try {
      await registerOpdVisit({
        patientId,
        // Omitted for a scoped caller on purpose: the server fills in their own
        // clinic, and sending it back would only create a way to disagree.
        department: mustChooseClinic ? department : undefined,
        doctorId: chosenDoctor || undefined,
        visitType,
        chiefComplaint: complaint.trim() || undefined,
        // Dedupes a REPLAY of this request — the session-refresh retry path in
        // apiFetch re-sends the POST after a 401. It does NOT dedupe a second
        // deliberate tap; the disabled `busy` button is what stops that, and
        // registering the same patient twice in one day is legitimate anyway
        // (two clinics, two tokens).
        idempotencyKey: `opd-${patientId}-${Date.now()}`,
      })
      setPatientId('')
      setComplaint('')
      onRegistered()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="fd-card fd-register" onSubmit={submit}>
      <h3 className="fd-card__title">Register a visit</h3>
      {mustChooseClinic && (
        <div className="fd-field">
          <label htmlFor="fd-department">Clinic</label>
          <select
            id="fd-department"
            value={department}
            onChange={e => setDepartment(e.target.value)}
            required
          >
            <option value="">Select a clinic…</option>
            {clinics.map(c => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </div>
      )}
      <div className="fd-field">
        <label htmlFor="fd-patient">Patient</label>
        <select
          id="fd-patient"
          value={patientId}
          onChange={e => setPatientId(e.target.value)}
          required
        >
          <option value="">Select a patient…</option>
          {patients.map(p => (
            <option key={p.id} value={p.id}>
              {p.name} ({p.mrn})
            </option>
          ))}
        </select>
      </div>
      <div className="fd-field">
        <label htmlFor="fd-doctor">Consulting doctor</label>
        <select id="fd-doctor" value={chosenDoctor} onChange={e => setDoctorId(e.target.value)}>
          <option value="">{clinic ? 'Unassigned' : 'Choose a clinic first'}</option>
          {clinicDoctors.map(d => (
            <option key={d.id} value={d.id}>{doctorLabel(d, clinicDoctors)}</option>
          ))}
        </select>
      </div>
      <div className="fd-field">
        <label htmlFor="fd-type">Visit type</label>
        <select
          id="fd-type"
          value={visitType}
          onChange={e => setVisitType(e.target.value as 'new' | 'follow_up' | 'review')}
        >
          <option value="new">New case</option>
          <option value="follow_up">Follow-up</option>
          <option value="review">Review</option>
        </select>
      </div>
      <div className="fd-field">
        <label htmlFor="fd-complaint">Chief complaint</label>
        <input
          id="fd-complaint"
          type="text"
          value={complaint}
          onChange={e => setComplaint(e.target.value)}
          placeholder="e.g. chest pain since morning"
        />
      </div>
      {error && <p className="fd-error" role="alert">{error}</p>}
      <button
        type="submit"
        className="fd-btn fd-btn--primary"
        disabled={!patientId || busy || (mustChooseClinic && !department)}
      >
        {busy ? 'Registering…' : 'Register & issue token'}
      </button>
    </form>
  )
}

// ── OPD queue ────────────────────────────────────────────────────────────────

function OpdQueue({
  queue,
  onAction,
  canRunConsultation = true,
}: {
  queue: OpdQueueResponse
  onAction: () => void
  /** False for a desk-only account: the server refuses it Call in and Complete,
   *  so they are not offered. No-show stays. */
  canRunConsultation?: boolean
}) {
  const [busyId, setBusyId] = useState('')
  const [error, setError] = useState('')
  const [lastCharge, setLastCharge] = useState<OpdVisit | null>(null)

  const run = async (visitId: string, fn: () => Promise<OpdVisit>) => {
    setBusyId(visitId)
    setError('')
    try {
      const result = await fn()
      if (result.consultation_charge !== undefined) setLastCharge(result)
      onAction()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusyId('')
    }
  }

  // Rendered before the empty-state check: completing the last patient of the
  // day empties the queue, and the note confirming their charge used to vanish
  // with it.
  const chargeNote = lastCharge?.consultation_charge && (
    <div className="fd-charge-note" role="status">
      <span className="fd-charge-note__label">Consultation charge raised</span>
      <span className="fd-money">
        {lastCharge.consultation_charge.rate_is_configured
          ? formatInrOrUnset(lastCharge.consultation_charge.total_paise)
          : formatInrOrUnset(null)}
      </span>
      {!lastCharge.consultation_charge.rate_is_configured && (
        <span className="fd-charge-note__hint">
          Nobody has priced {lastCharge.consultation_charge.service_code} yet, the
          charge is recorded and waiting for a rate. It is not a free consultation.
        </span>
      )}
    </div>
  )

  if (!queue.queues.length) {
    return (
      <div className="fd-queues">
        {error && <p className="fd-error" role="alert">{error}</p>}
        {chargeNote}
        <EmptyNote>No one is waiting in {queue.department ?? 'any clinic'} today.</EmptyNote>
      </div>
    )
  }

  return (
    <div className="fd-queues">
      {error && <p className="fd-error" role="alert">{error}</p>}
      {chargeNote}

      {queue.queues.map(q => (
        <section className="fd-card fd-queue" key={`${q.department}:${q.doctor_id ?? 'none'}`}>
          <header className="fd-queue__head">
            <div>
              <h3 className="fd-card__title">{q.doctor_name}</h3>
              <p className="fd-queue__sub">{q.department}</p>
            </div>
            <div className="fd-queue__stats">
              <span className="fd-stat">
                <span className="fd-stat__value">{q.waiting_count}</span>
                <span className="fd-stat__label">waiting</span>
              </span>
              <span className="fd-stat">
                <span className="fd-stat__value">{formatWait(q.longest_wait_minutes)}</span>
                <span className="fd-stat__label">longest wait</span>
              </span>
            </div>
          </header>

          <div className="fd-table-scroll">
            <table className="fd-table">
              <thead>
                <tr>
                  <th scope="col">Token</th>
                  <th scope="col">Patient</th>
                  <th scope="col">Complaint</th>
                  <th scope="col">Waiting</th>
                  <th scope="col">Status</th>
                  <th scope="col">Action</th>
                </tr>
              </thead>
              <tbody>
                {q.visits.map(v => (
                  <tr key={v.id}>
                    <td className="fd-token">{v.token_number}</td>
                    <td>
                      <span className="fd-patient-name">{v.patient_name ?? v.patient_id}</span>
                      <span className="fd-patient-mrn">{v.patient_mrn}</span>
                    </td>
                    <td className="fd-complaint" title={v.chief_complaint ?? ''}>
                      {v.chief_complaint || '—'}
                    </td>
                    <td><WaitPill minutes={v.waiting_minutes} /></td>
                    <td><StatusChip status={v.status} /></td>
                    <td className="fd-actions-cell">
                      <div className="fd-actions">
                        {canRunConsultation && v.status === 'waiting' && (
                          <button
                            type="button"
                            className="fd-btn"
                            disabled={busyId === v.id}
                            onClick={() => run(v.id, () => startOpdConsultation(v.id))}
                          >
                            Call in
                          </button>
                        )}
                        {canRunConsultation && v.status === 'in_consultation' && (
                          <button
                            type="button"
                            className="fd-btn fd-btn--primary"
                            disabled={busyId === v.id}
                            onClick={() => run(v.id, () => completeOpdVisit(v.id))}
                          >
                            Complete
                          </button>
                        )}
                        {(v.status === 'waiting' || v.status === 'in_consultation') && (
                          <button
                            type="button"
                            className="fd-btn fd-btn--quiet"
                            disabled={busyId === v.id}
                            onClick={() => run(v.id, () => cancelOpdVisit(v.id, 'Left without being seen'))}
                          >
                            No-show
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ))}
    </div>
  )
}

// ── Transfer modal ───────────────────────────────────────────────────────────

function TransferModal({
  patient,
  destinations,
  onClose,
  onDone,
}: {
  patient: WardPatient
  destinations: TransferDestination[]
  onClose: () => void
  onDone: () => void
}) {
  const [toDepartment, setToDepartment] = useState('')
  const [toWard, setToWard] = useState('')
  const [toBed, setToBed] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const close = useCallback(() => { if (!busy) onClose() }, [busy, onClose])
  const modalRef = useFocusTrap<HTMLDivElement>(close)

  const wards = destinations.find(d => d.department === toDepartment)?.wards ?? []
  const canSubmit = !busy && toDepartment !== '' && reason.trim().length >= 4

  const submit = async () => {
    if (!canSubmit) return
    setBusy(true)
    setError('')
    try {
      await transferPatient({
        patientId: patient.id,
        toDepartment,
        toWard: toWard.trim() || undefined,
        toBed: toBed.trim() || undefined,
        reason: reason.trim(),
        idempotencyKey: `transfer-${patient.id}-${Date.now()}`,
      })
      onDone()
    } catch (err) {
      setError(errorText(err))
      setBusy(false)
    }
  }

  return createPortal(
    <div className="fd-modal-backdrop" onClick={close}>
      <div
        className="fd-modal"
        ref={modalRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="fd-transfer-title"
        onClick={e => e.stopPropagation()}
      >
        <h2 className="fd-modal__title" id="fd-transfer-title">
          Transfer {patient.name}
        </h2>
        <p className="fd-modal__lede">
          Currently {patient.ward || 'unassigned ward'}
          {patient.bed ? `, bed ${patient.bed}` : ''}. The admission stays open, same
          admission, same orders, new location.
        </p>

        <div className="fd-field">
          <label htmlFor="fd-to-dept">Receiving department</label>
          <select
            id="fd-to-dept"
            value={toDepartment}
            onChange={e => { setToDepartment(e.target.value); setToWard('') }}
            disabled={busy}
          >
            <option value="">Select a department…</option>
            {destinations.map(d => (
              <option key={d.department} value={d.department}>{d.department}</option>
            ))}
          </select>
          <p className="fd-hint">
            Only departments with clinical staff appear here. A ward nobody covers
            would leave this patient admitted and on no one&rsquo;s board.
          </p>
        </div>

        <div className="fd-field-row">
          <div className="fd-field">
            <label htmlFor="fd-to-ward">Ward / bay</label>
            <input
              id="fd-to-ward"
              list="fd-ward-options"
              value={toWard}
              onChange={e => setToWard(e.target.value)}
              disabled={busy || !toDepartment}
              placeholder="e.g. ICU Bay 2"
            />
            <datalist id="fd-ward-options">
              {wards.map(w => <option key={w.ward} value={w.ward} />)}
            </datalist>
          </div>
          <div className="fd-field">
            <label htmlFor="fd-to-bed">Bed</label>
            <input
              id="fd-to-bed"
              value={toBed}
              onChange={e => setToBed(e.target.value)}
              disabled={busy}
              placeholder="e.g. I-4"
            />
          </div>
        </div>

        <div className="fd-field">
          <label htmlFor="fd-reason">Reason for transfer</label>
          <textarea
            id="fd-reason"
            rows={2}
            value={reason}
            onChange={e => setReason(e.target.value)}
            disabled={busy}
            placeholder="e.g. Requires ventilatory support"
          />
        </div>

        {/* The consequence, stated before the click and not after it. */}
        <div className="fd-handover" role="note">
          <strong>This hands the chart over.</strong> As soon as the transfer is
          recorded, staff in the receiving department can open this patient&rsquo;s
          record and staff in {patient.admission_department || 'the current department'}{' '}
          can no longer see them.
        </div>

        {error && <p className="fd-error" role="alert">{error}</p>}

        <div className="fd-modal__actions">
          <button type="button" className="fd-btn fd-btn--quiet" onClick={close} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="fd-btn fd-btn--primary"
            onClick={submit}
            disabled={!canSubmit}
          >
            {busy ? 'Transferring…' : 'Record transfer'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

// ── Transfers view ───────────────────────────────────────────────────────────

function TransfersView({
  patients,
  destinations,
  onTransferred,
}: {
  patients: WardPatient[]
  destinations: TransferDestination[]
  onTransferred: () => void
}) {
  const [selected, setSelected] = useState<WardPatient | null>(null)
  const [historyFor, setHistoryFor] = useState<string>('')
  const [movements, setMovements] = useState<PatientMovement[]>([])
  const [historyError, setHistoryError] = useState('')

  const showHistory = async (patient: WardPatient) => {
    setHistoryFor(patient.id)
    setHistoryError('')
    try {
      const result = await fetchPatientMovements(patient.id)
      setMovements(result.movements)
    } catch (err) {
      setMovements([])
      setHistoryError(errorText(err))
    }
  }

  if (!patients.length) {
    return <EmptyNote>No admitted patients on your ward to transfer.</EmptyNote>
  }

  return (
    <div className="fd-transfers">
      <section className="fd-card">
        <h3 className="fd-card__title">Admitted patients</h3>
        <div className="fd-table-scroll">
          <table className="fd-table">
            <thead>
              <tr>
                <th scope="col">Patient</th>
                <th scope="col">MRN</th>
                <th scope="col">Ward</th>
                <th scope="col">Bed</th>
                <th scope="col">Action</th>
              </tr>
            </thead>
            <tbody>
              {patients.map(p => (
                <tr key={p.id}>
                  <td><span className="fd-patient-name">{p.name}</span></td>
                  <td className="fd-mono">{p.mrn}</td>
                  <td className="fd-nowrap">{p.ward || '—'}</td>
                  <td className="fd-mono">{p.bed || '—'}</td>
                  <td className="fd-actions-cell">
                    <div className="fd-actions">
                      <button
                        type="button"
                        className="fd-btn fd-btn--primary"
                        onClick={() => setSelected(p)}
                      >
                        Transfer
                      </button>
                      <button
                        type="button"
                        className="fd-btn fd-btn--quiet"
                        onClick={() => showHistory(p)}
                      >
                        History
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {historyFor && (
        <section className="fd-card">
          <h3 className="fd-card__title">Movement history</h3>
          {historyError && <p className="fd-error" role="alert">{historyError}</p>}
          {!historyError && movements.length === 0 && (
            <EmptyNote>No recorded moves for this patient.</EmptyNote>
          )}
          <ol className="fd-timeline">
            {movements.map(m => (
              <li key={m.id} className="fd-timeline__item">
                <span className="fd-timeline__when">
                  {formatClinicalDateTime(m.moved_at)}
                </span>
                <span className="fd-timeline__move">
                  {m.from_department || 'Unknown'}
                  {m.from_bed ? ` (${m.from_bed})` : ''} → {m.to_department}
                  {m.to_bed ? ` (${m.to_bed})` : ''}
                </span>
                <span className="fd-timeline__reason">{m.reason}</span>
                {m.moved_by_name && (
                  <span className="fd-timeline__who">by {m.moved_by_name}</span>
                )}
              </li>
            ))}
          </ol>
        </section>
      )}

      {selected && (
        <TransferModal
          patient={selected}
          destinations={destinations}
          onClose={() => setSelected(null)}
          onDone={() => { setSelected(null); onTransferred() }}
        />
      )}
    </div>
  )
}

// ── Panel ────────────────────────────────────────────────────────────────────

interface FrontDeskPanelProps {
  /** Tests only: the desk views to offer. The shell mounts the panel without
   *  props, which means every view found in ./frontdesk/. */
  views?: readonly FrontDeskViewEntry[]
  /** Tests only: whether the signed-in user holds a permission. */
  holds?: (permission: string) => boolean
}

export default function FrontDeskPanel({
  views = INSTALLED_DESK_VIEWS,
  holds = roleHolds,
}: FrontDeskPanelProps = {}) {
  const [view, setView] = useState<View>('opd')
  const [queue, setQueue] = useState<OpdQueueResponse | null>(null)
  const [patients, setPatients] = useState<WardPatient[]>([])
  const [destinations, setDestinations] = useState<TransferDestination[]>([])
  const [roster, setRoster] = useState<ConsultingDoctor[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const mounted = useRef(true)

  /* The registration feature, as the shell's health probe last read it. A
     view that meets `feature_disabled` later sets `featureGone`, and the desk
     drops back to its two built-in sections without a reload. */
  const features = useSyncExternalStore(subscribeModuleFlags, currentFeatureFlags)
  const [featureGone, setFeatureGone] = useState(false)
  const registrationOn = features.registration === true && !featureGone
  /* A desk-only account (registration.* and no chart): the clinical board and
     the transfer list would refuse it, so they are not asked for. With the
     feature off nobody is desk-only, and the desk loads what it always did. */
  const deskOnly = registrationOn && !holds(PATIENT_READ_ALL)
  const deskViews = useMemo(
    () => (registrationOn ? visibleFrontDeskViews(views, holds) : []),
    [registrationOn, views, holds],
  )
  const activeDeskView = deskViews.find(v => v.id === view) ?? null
  /* The section on screen. A desk view that went away (the feature switched
     off) or Transfers for a desk-only account falls back to the OPD queue. */
  const current: View = activeDeskView
    ? activeDeskView.id
    : view === 'transfers' && !deskOnly ? 'transfers' : 'opd'
  const openView = useCallback((id: string) => {
    if (id === 'opd' || (id === 'transfers' && !deskOnly) || deskViews.some(v => v.id === id)) {
      setView(id)
    }
  }, [deskOnly, deskViews])
  const featureOff = useCallback(() => setFeatureGone(true), [])

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const load = useCallback(async () => {
    setError('')
    try {
      if (deskOnly) {
        const [q, r] = await Promise.all([fetchOpdQueue(), fetchConsultingDoctors()])
        if (!mounted.current) return
        setQueue(q)
        setPatients([])
        setDestinations([])
        setRoster(Array.isArray(r.doctors) ? r.doctors : [])
        return
      }
      const [q, p, d, r] = await Promise.all([
        fetchOpdQueue(),
        apiFetch<WardPatient[]>('/vigil/patients?limit=200'),
        fetchTransferDestinations(),
        fetchConsultingDoctors(),
      ])
      if (!mounted.current) return
      setQueue(q)
      setPatients(Array.isArray(p) ? p : [])
      setDestinations(d.destinations)
      setRoster(Array.isArray(r.doctors) ? r.doctors : [])
    } catch (err) {
      if (!mounted.current) return
      setError(errorText(err))
    } finally {
      if (mounted.current) setLoading(false)
    }
  }, [deskOnly])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { load() }, [load])
  // A patient admitted from the shell can be transferred without a page reload.
  useDataChanged(() => { void load() })

  /* The OPD-mode queue's own re-read (opd-8): the queue alone, so its poll
     does not also re-read the ward list and the transfer destinations. */
  const refreshQueue = useCallback(async () => {
    try {
      const q = await fetchOpdQueue()
      if (mounted.current) setQueue(q)
    } catch (err) {
      if (mounted.current) setError(errorText(err))
    }
  }, [])

  /**
   * Who the desk may book a visit with: the staff roster.
   *
   * This list used to be derived ONLY from `queue.queues` (the visits already
   * registered today), which is circular: on an empty morning no doctor had a
   * queue, so the first visit of the day could not be assigned to anyone. The
   * roster (`/frontdesk/opd/doctors`) answers "who works here", which is the
   * question this dropdown asks.
   *
   * Doctors who hold a queue but have left the roster (deactivated, or moved
   * to another department) are no longer folded in: the server refuses a new
   * booking with a doctor who is not an active clinician of that clinic, so
   * offering them would only produce a refusal. Their waiting patients still
   * show in the queue below.
   */
  const doctors = roster

  if (loading) return <div className="orb-panel-loading">Loading the front desk…</div>

  if (error && !queue) {
    return (
      <div className="orb-panel-error" role="alert">
        <p>{error}</p>
        <button type="button" onClick={load}>Retry</button>
      </div>
    )
  }

  return (
    <div className="orb-panel fd-panel">
      <header className="fd-header">
        <div>
          <h2 className="fd-title">Front Desk</h2>
          <p className="fd-subtitle">
            {deskOnly ? 'Outpatient registration' : 'Outpatient registration and ward transfers'}
            {queue?.department ? ` (${queue.department})` : ''}
          </p>
        </div>
        <div className="fd-viewswitch" role="tablist" aria-label="Front desk sections">
          <button
            type="button"
            role="tab"
            aria-selected={current === 'opd'}
            className={`fd-tab ${current === 'opd' ? 'fd-tab--active' : ''}`}
            onClick={() => setView('opd')}
          >
            OPD queue
          </button>
          {!deskOnly && (
            <button
              type="button"
              role="tab"
              aria-selected={current === 'transfers'}
              className={`fd-tab ${current === 'transfers' ? 'fd-tab--active' : ''}`}
              onClick={() => setView('transfers')}
            >
              Transfers
            </button>
          )}
          {deskViews.map(v => (
            <button
              key={v.id}
              type="button"
              role="tab"
              aria-selected={current === v.id}
              className={`fd-tab ${current === v.id ? 'fd-tab--active' : ''}`}
              onClick={() => setView(v.id)}
            >
              {v.label}
            </button>
          ))}
        </div>
      </header>

      {error && <p className="fd-error" role="alert">{error}</p>}

      {activeDeskView ? (
        <activeDeskView.Component
          scopedDepartment={queue?.department ?? null}
          openView={openView}
          onFeatureOff={featureOff}
        />
      ) : current === 'transfers' ? (
        <TransfersView
          patients={patients}
          destinations={destinations}
          onTransferred={load}
        />
      ) : (
        <div className="fd-opd">
          {registrationOn ? (
            /* opd-6a: the server refuses a visit without the identity check,
               so the ward-list form is not offered; the Register view is. */
            <RegisterVisitPointer
              onOpen={deskViews.some(v => v.id === REGISTER_VIEW_ID)
                ? () => openView(REGISTER_VIEW_ID)
                : null}
            />
          ) : (
            <RegisterVisitForm
              patients={patients}
              doctors={doctors}
              clinics={destinations.map(d => d.department)}
              /* The SERVER's answer about the caller, not the signed-in user
                 object: `queue.department` is what `department_scope` derived, so
                 the field appears exactly when the server would demand it. */
              scopedDepartment={queue?.department ?? null}
              onRegistered={load}
            />
          )}
          {queue && (isOpdModeQueue(queue)
            /* The server's 'opd' feature is on: priority, room, skipped and
               the coded "left without being seen" (opd-8). Calling in needs
               opd.consult; finishing is on the doctor's Outpatients tab. */
            ? <QueueView queue={queue} onRefresh={refreshQueue} canCallIn={holds(OPD_CONSULT)} />
            : <OpdQueue queue={queue} onAction={load} canRunConsultation={!deskOnly} />)}
        </div>
      )}
    </div>
  )
}
