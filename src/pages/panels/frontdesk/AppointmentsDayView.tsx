/**
 * Front Desk, Appointments (opd-16): one hospital day of appointments for this
 * desk's clinic, the slots each doctor's clinic offers that day and how many
 * are taken, and what the desk does with a booking: book a free slot, check the
 * patient in, mark a no-show, move it to another time, or cancel it.
 *
 * Shown while the 'registration' feature is on (the panel discovers this file
 * through ./viewRegistry.ts), and USED while 'opd' is on too: with outpatients
 * switched off the view says so and asks the server for nothing. The server
 * decides every refusal; this screen only hides what a role cannot do.
 *
 * TIME. Days are hospital calendar days (formatClinicalDateKey) and times are
 * hospital wall-clock times (formatClinicalClock); the server computes slots,
 * which have started and which bookings read as a no-show, in hospital time.
 * Nothing here works out a date from the browser's clock.
 *
 * WHO THE PATIENT IS (ruling OPD-S1). Check-in asks the patient to confirm who
 * they are (IdentityConfirmDialog): the desk types what they state, and the
 * server answers match or no match. The stored date of birth never reaches this
 * screen.
 */
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import {
  bookAppointment,
  cancelAppointment,
  checkInAppointment,
  fetchAppointmentsDay,
  markNoShow,
  rescheduleAppointment,
  type AppointmentStatus,
  type AppointmentsDay,
  type DaySession,
  type DaySlot,
  type DeskAppointment,
} from '../../../api/appointmentsDesk'
import {
  errorDetail,
  isFeatureDisabled,
  newRequestKey,
  type IndexCard,
  type StatedIdentity,
} from '../../../api/registration'
import { useFocusTrap } from '../../../hooks/useFocusTrap'
import { formatClinicalClock, formatClinicalDateKey } from '../../../lib/datetime'
import { PATIENT_WRITE, REGISTRATION_WRITE, hasPermission } from '../../../lib/roles'
import { currentFeatureFlags, subscribeModuleFlags } from '../../../shellConfig'
import IdentityConfirmDialog from './IdentityConfirmDialog'
import PatientIndexSearch from './PatientIndexSearch'
import { cardMeta } from './indexCard'
import type { FrontDeskViewProps } from './viewRegistry'
import './AppointmentsDayView.css'

export const viewId = 'appointments'
export const viewLabel = 'Appointments'
export const viewOrder = 20
export const viewPermission = 'registration.read'

const STATUS_TEXT: Record<AppointmentStatus, string> = {
  booked: 'Booked',
  checked_in: 'Checked in',
  completed: 'Seen',
  cancelled: 'Cancelled',
  no_show: 'Did not come',
}

function errorText(err: unknown): string {
  if (err instanceof Error && err.message) return err.message
  return 'Something went wrong. Nothing was saved.'
}

/** Which feature a 'feature_disabled' answer names. */
function disabledFeature(err: unknown): string | null {
  if (!isFeatureDisabled(err)) return null
  const feature = errorDetail(err)?.feature
  return typeof feature === 'string' ? feature : 'opd'
}

function useOpdOn(): boolean {
  return useSyncExternalStore(subscribeModuleFlags, () => currentFeatureFlags().opd === true)
}

function slotText(slot: DaySlot): string {
  if (slot.past) return 'Started'
  if (slot.free <= 0) return 'Full'
  return slot.capacity > 1 ? `${slot.free} of ${slot.capacity} free` : 'Free'
}

function statusOf(a: DeskAppointment): { status: AppointmentStatus; text: string } {
  const status = a.effective_status ?? a.status
  if (a.no_show_computed) return { status, text: 'Did not come (clinic ended)' }
  return { status, text: STATUS_TEXT[status] ?? status }
}

// ── Booking a slot ───────────────────────────────────────────────────────────

function BookDialog({
  day,
  session,
  slot,
  types,
  onBooked,
  onClose,
  onOff,
}: {
  day: string
  session: DaySession
  slot: DaySlot
  types: { key: string; label: string }[]
  onBooked: () => void
  onClose: () => void
  onOff: (err: unknown) => boolean
}) {
  const [patient, setPatient] = useState<IndexCard | null>(null)
  const [type, setType] = useState(types[0]?.key ?? 'follow_up')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  // One request key per patient chosen: a retry after a lost answer replays
  // that patient's booking, and another patient is a new request (the server
  // refuses a key that already names a different booking).
  const [key, setKey] = useState(() => newRequestKey('appointment'))
  const close = useCallback(() => { if (!busy) onClose() }, [busy, onClose])
  const ref = useFocusTrap<HTMLDivElement>(close)

  const choosePatient = useCallback((card: IndexCard | null) => {
    setPatient(card)
    setKey(newRequestKey('appointment'))
    setError('')
  }, [])

  const book = async () => {
    if (!patient || busy) return
    setBusy(true)
    setError('')
    try {
      await bookAppointment({
        patient_id: patient.patient_id,
        scheduled_date: day,
        scheduled_time: slot.start,
        doctor_id: session.doctor_id,
        appointment_type: type,
        reason: reason.trim() || null,
      }, key)
      onBooked()
    } catch (err) {
      if (onOff(err)) return
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return createPortal(
    <div className="fd-modal-backdrop" onClick={close}>
      <div className="fd-modal" ref={ref} role="dialog" aria-modal="true"
        aria-labelledby="adv-book-title" onClick={e => e.stopPropagation()}>
        <h2 className="fd-modal__title" id="adv-book-title">Book this slot</h2>
        <p className="fd-modal__lede">
          {session.doctor_name ?? 'The doctor'}, {formatClinicalDateKey(day)} at{' '}
          {formatClinicalClock(slot.start)}{session.room ? `, ${session.room}` : ''}.
        </p>
        {patient ? (
          <p className="adv-chosen">
            <span className="adv-strong">{patient.name}</span>
            <span className="adv-sub">{cardMeta(patient)}</span>
          </p>
        ) : (
          <PatientIndexSearch onChoose={choosePatient} />
        )}
        {patient && (
          <>
            <div className="fd-field">
              <label htmlFor="adv-book-type">Type</label>
              <select id="adv-book-type" value={type} onChange={e => setType(e.target.value)}>
                {types.map(t => <option key={t.key} value={t.key}>{t.label}</option>)}
              </select>
            </div>
            <div className="fd-field">
              <label htmlFor="adv-book-reason">Reason (optional)</label>
              <input id="adv-book-reason" type="text" maxLength={2000} value={reason}
                onChange={e => setReason(e.target.value)} placeholder="What the visit is for" />
            </div>
          </>
        )}
        {error && <p className="fd-error" role="alert">{error}</p>}
        <div className="fd-modal__actions">
          {patient && (
            <button type="button" className="fd-btn fd-btn--quiet" disabled={busy}
              onClick={() => choosePatient(null)}>Choose another patient</button>
          )}
          <button type="button" className="fd-btn fd-btn--quiet" disabled={busy} onClick={close}>
            Close
          </button>
          <button type="button" className="fd-btn fd-btn--primary" disabled={!patient || busy}
            onClick={book}>{busy ? 'Booking…' : 'Book'}</button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

// ── One booking's actions ───────────────────────────────────────────────────

function RescheduleForm({
  appointment,
  onDone,
  onCancel,
  onOff,
}: {
  appointment: DeskAppointment
  onDone: () => void
  onCancel: () => void
  onOff: (err: unknown) => boolean
}) {
  const [date, setDate] = useState(appointment.scheduled_date)
  const [time, setTime] = useState(appointment.scheduled_time)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [key] = useState(() => newRequestKey('reschedule'))
  const id = appointment.id

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    setError('')
    try {
      await rescheduleAppointment(id, {
        scheduled_date: date, scheduled_time: time, reason: reason.trim() || undefined,
      }, key)
      onDone()
    } catch (err) {
      if (onOff(err)) return
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="adv-inline" onSubmit={submit} aria-label="Move this appointment">
      <div className="fd-field">
        <label htmlFor={`adv-rs-date-${id}`}>New day</label>
        <input id={`adv-rs-date-${id}`} type="date" value={date} required
          onChange={e => setDate(e.target.value)} />
      </div>
      <div className="fd-field">
        <label htmlFor={`adv-rs-time-${id}`}>New time</label>
        <input id={`adv-rs-time-${id}`} type="time" value={time} required
          onChange={e => setTime(e.target.value)} />
      </div>
      <div className="fd-field">
        <label htmlFor={`adv-rs-reason-${id}`}>Reason (optional)</label>
        <input id={`adv-rs-reason-${id}`} type="text" maxLength={500} value={reason}
          onChange={e => setReason(e.target.value)} />
      </div>
      <button type="button" className="fd-btn fd-btn--quiet" disabled={busy} onClick={onCancel}>
        Keep the time
      </button>
      <button type="submit" className="fd-btn fd-btn--primary" disabled={busy}>
        {busy ? 'Moving…' : 'Move'}
      </button>
      {error && <p className="fd-error" role="alert">{error}</p>}
    </form>
  )
}

// ── The view ─────────────────────────────────────────────────────────────────

interface AppointmentsDayViewProps extends FrontDeskViewProps {
  /** Tests only: whether this account may book and move appointments. The
   *  shell mounts the view without it, which reads the client permission
   *  mirror (patient.write or registration.write). */
  canBook?: boolean
}

export default function AppointmentsDayView({
  onFeatureOff: reportFeatureOff,
  canBook = hasPermission(PATIENT_WRITE) || hasPermission(REGISTRATION_WRITE),
}: AppointmentsDayViewProps) {
  const opdOn = useOpdOn()
  const [switchedOff, setSwitchedOff] = useState(false)
  const [day, setDay] = useState('')
  const [doctorId, setDoctorId] = useState('')
  const [data, setData] = useState<AppointmentsDay | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [actionError, setActionError] = useState('')
  const [notice, setNotice] = useState('')
  const [booking, setBooking] = useState<{ session: DaySession; slot: DaySlot } | null>(null)
  const [checkingIn, setCheckingIn] = useState<DeskAppointment | null>(null)
  const [moving, setMoving] = useState('')
  const [cancelling, setCancelling] = useState('')
  const [cancelReason, setCancelReason] = useState('')
  const [busyId, setBusyId] = useState('')
  const live = useRef(true)

  useEffect(() => {
    live.current = true
    return () => { live.current = false }
  }, [])

  /* A 'feature_disabled' answer: outpatients went off (say so here, keep the
     other desk views), or registration did (hand back to the panel). */
  const onOff = useCallback((err: unknown): boolean => {
    const feature = disabledFeature(err)
    if (!feature) return false
    setSwitchedOff(true)
    if (feature === 'registration') reportFeatureOff()
    return true
  }, [reportFeatureOff])

  const load = useCallback(async (forDay: string, forDoctor: string) => {
    try {
      const answer = await fetchAppointmentsDay(forDay || undefined, forDoctor || undefined)
      if (!live.current) return
      setData(answer)
      setDay(answer.date)
      setError('')
    } catch (err) {
      if (!live.current) return
      if (onOff(err)) return
      setError(errorText(err))
    } finally {
      if (live.current) setLoading(false)
    }
  }, [onOff])

  useEffect(() => {
    if (!opdOn) return
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger: the loader sets state only after its request answers.
    void load('', '')
  }, [opdOn, load])

  const reload = useCallback(() => { void load(day, doctorId) }, [load, day, doctorId])

  const doctors = useMemo(() => {
    const seen = new Map<string, string>()
    for (const s of data?.sessions ?? []) seen.set(s.doctor_id, s.doctor_name ?? s.doctor_id)
    for (const a of data?.appointments ?? []) {
      if (a.doctor_id) seen.set(a.doctor_id, a.doctor_name ?? a.doctor_id)
    }
    return [...seen.entries()].sort((x, y) => x[1].localeCompare(y[1]))
  }, [data])

  const act = async (id: string, run: () => Promise<unknown>, done: string) => {
    setBusyId(id)
    setActionError('')
    setNotice('')
    try {
      await run()
      if (!live.current) return
      setNotice(done)
      reload()
    } catch (err) {
      if (onOff(err)) return
      if (live.current) setActionError(errorText(err))
    } finally {
      if (live.current) setBusyId('')
    }
  }

  const confirmCheckIn = async (stated: StatedIdentity) => {
    const target = checkingIn
    setCheckingIn(null)
    if (!target) return
    setBusyId(target.id)
    setActionError('')
    setNotice('')
    try {
      const result = await checkInAppointment(target.id, stated)
      if (!live.current) return
      setNotice(`${target.patient_name ?? 'The patient'} is checked in. Token ${result.visit.token_number}.`)
      reload()
    } catch (err) {
      if (onOff(err)) return
      if (live.current) setActionError(errorText(err))
    } finally {
      if (live.current) setBusyId('')
    }
  }

  if (!opdOn || switchedOff) {
    return (
      <section className="adv-view" aria-label="Appointments">
        <p className="fd-empty">
          Booking with a doctor and a slot is part of outpatients, which is switched off on this
          site.
        </p>
      </section>
    )
  }

  const today = data?.hospital_today ?? ''
  const isToday = day !== '' && day === today

  return (
    <section className="adv-view" aria-label="Appointments">
      <div className="adv-head">
        <div>
          <h3 className="fd-card__title">
            {day ? `Appointments, ${formatClinicalDateKey(day)}` : 'Appointments'}
          </h3>
          <span className="adv-sub">
            {data?.department ? data.department : 'Every clinic'}
            {isToday ? ', today' : ''}
          </span>
        </div>
        <div className="adv-head__controls">
          <div className="fd-field">
            <label htmlFor="adv-day">Day</label>
            <input id="adv-day" type="date" value={day}
              onChange={e => { setDay(e.target.value); if (e.target.value) void load(e.target.value, doctorId) }} />
          </div>
          <div className="fd-field">
            <label htmlFor="adv-doctor">Doctor</label>
            <select id="adv-doctor" value={doctorId}
              onChange={e => { setDoctorId(e.target.value); void load(day, e.target.value) }}>
              <option value="">Every doctor</option>
              {doctors.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
            </select>
          </div>
          <button type="button" className="fd-btn fd-btn--quiet" onClick={reload}>Refresh</button>
        </div>
      </div>

      {error && <p className="fd-error" role="alert">{error}</p>}
      {actionError && <p className="fd-error" role="alert">{actionError}</p>}
      {notice && <p className="adv-done" role="status">{notice}</p>}

      <div className="fd-card">
        <h3 className="fd-card__title">Clinics this day</h3>
        {loading && !data ? (
          <p className="fd-hint">Loading…</p>
        ) : (data?.sessions.length ?? 0) === 0 ? (
          <p className="fd-empty">No clinic runs this day.</p>
        ) : (
          <ul className="adv-sessions">
            {data!.sessions.map(s => (
              <li key={s.session_id} className="adv-session">
                <span className="adv-strong">{s.doctor_name ?? s.doctor_id}</span>
                <span className="adv-sub">
                  {formatClinicalClock(s.start_time)} to {formatClinicalClock(s.end_time)}
                  {s.room ? `, ${s.room}` : ''}
                </span>
                {s.cancelled ? (
                  <p className="adv-off">
                    Not running{s.cancel_reason ? `: ${s.cancel_reason}` : ''}.
                  </p>
                ) : (
                  <ul className="adv-slots" aria-label={`Slots with ${s.doctor_name ?? s.doctor_id}`}>
                    {s.slots.map(slot => {
                      const open = !slot.past && slot.free > 0
                      const label = `${formatClinicalClock(slot.start)}, ${slotText(slot)}`
                      return (
                        <li key={slot.start}>
                          {open && canBook ? (
                            <button type="button" className="adv-slot" aria-label={`Book ${label}`}
                              onClick={() => setBooking({ session: s, slot })}>
                              {formatClinicalClock(slot.start)}
                              <span className="adv-slot__free">{slotText(slot)}</span>
                            </button>
                          ) : (
                            <span className={`adv-slot${open ? '' : ' adv-slot--closed'}`}>
                              {formatClinicalClock(slot.start)}
                              <span>{slotText(slot)}</span>
                            </span>
                          )}
                        </li>
                      )
                    })}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="fd-card">
        <h3 className="fd-card__title">Booked patients</h3>
        {loading && !data ? (
          <p className="fd-hint">Loading…</p>
        ) : (data?.appointments.length ?? 0) === 0 ? (
          <p className="fd-empty">No appointments this day.</p>
        ) : (
          <div className="fd-table-scroll">
            <table className="fd-table">
              <thead>
                <tr>
                  <th scope="col">Time</th>
                  <th scope="col">Patient</th>
                  <th scope="col">Doctor</th>
                  <th scope="col">Type</th>
                  <th scope="col">Status</th>
                  <th scope="col"><span className="adv-sub">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                {data!.appointments.map(a => {
                  const { status, text } = statusOf(a)
                  const open = a.status === 'booked'
                  const busy = busyId === a.id
                  return (
                    <tr key={a.id}>
                      <td className="fd-nowrap">{formatClinicalClock(a.scheduled_time)}</td>
                      <td>
                        <span className="fd-patient-name">{a.patient_name ?? a.patient_id}</span>
                        <span className="adv-sub">
                          {a.patient_uhid ? `UHID ${a.patient_uhid}` : 'No UHID'}
                          {a.inpatient_now ? ', inpatient now' : ''}
                        </span>
                      </td>
                      <td>{a.doctor_name ?? <span className="adv-sub">No doctor</span>}</td>
                      <td>
                        {a.appointment_type_label}
                        {a.origin === 'patient' && <span className="adv-sub">Asked for by the patient</span>}
                      </td>
                      <td>
                        <span className={`adv-status adv-status--${status}`}>{text}</span>
                        {a.token_number != null && (
                          <span className="adv-sub">Token {a.token_number}</span>
                        )}
                      </td>
                      <td>
                        {canBook && (
                          <div className="adv-row-actions">
                            {open && isToday && (
                              <button type="button" className="fd-btn fd-btn--primary" disabled={busy}
                                onClick={() => setCheckingIn(a)}>Check in</button>
                            )}
                            {open && (
                              <button type="button" className="fd-btn fd-btn--quiet" disabled={busy}
                                onClick={() => act(a.id, () => markNoShow(a.id),
                                  'Marked as did not come.')}>Did not come</button>
                            )}
                            {(open || a.status === 'no_show') && (
                              <button type="button" className="fd-btn fd-btn--quiet" disabled={busy}
                                onClick={() => setMoving(moving === a.id ? '' : a.id)}>Move</button>
                            )}
                            {open && (
                              <button type="button" className="fd-btn fd-btn--quiet" disabled={busy}
                                onClick={() => { setCancelling(cancelling === a.id ? '' : a.id); setCancelReason('') }}>
                                Cancel
                              </button>
                            )}
                          </div>
                        )}
                        {moving === a.id && (
                          <RescheduleForm appointment={a} onOff={onOff}
                            onCancel={() => setMoving('')}
                            onDone={() => { setMoving(''); setNotice('Appointment moved.'); reload() }} />
                        )}
                        {cancelling === a.id && (
                          <div className="adv-inline">
                            <div className="fd-field">
                              <label htmlFor={`adv-cancel-${a.id}`}>Reason</label>
                              <input id={`adv-cancel-${a.id}`} type="text" maxLength={500}
                                value={cancelReason} onChange={e => setCancelReason(e.target.value)} />
                            </div>
                            <button type="button" className="fd-btn fd-btn--primary" disabled={busy}
                              onClick={() => act(a.id, () => cancelAppointment(a.id, cancelReason),
                                'Appointment cancelled.').then(() => setCancelling(''))}>
                              Cancel appointment
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {booking && data && (
        <BookDialog day={day} session={booking.session} slot={booking.slot} types={data.types}
          onOff={onOff} onClose={() => setBooking(null)}
          onBooked={() => { setBooking(null); setNotice('Appointment booked.'); reload() }} />
      )}
      {checkingIn && (
        <IdentityConfirmDialog
          patient={{
            patient_id: checkingIn.patient_id,
            name: checkingIn.patient_name ?? checkingIn.patient_id,
            uhid: checkingIn.patient_uhid,
            inpatient_now: checkingIn.inpatient_now,
          }}
          onConfirmed={confirmCheckIn}
          onClose={() => setCheckingIn(null)}
        />
      )}
    </section>
  )
}
