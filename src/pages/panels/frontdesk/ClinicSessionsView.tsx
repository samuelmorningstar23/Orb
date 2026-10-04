/**
 * Front Desk, Clinic timings (opd-15): each doctor's weekly outpatient clinics,
 * the days a clinic does not run, and the slots a clinic offers on a given day.
 *
 * Shown only while the 'registration' feature is on (the panel discovers this
 * file through ./viewRegistry.ts). The desk reads it; only an account holding
 * schedule.manage (the administrator) sees the forms that change it, and the
 * server refuses anyone else whatever this screen shows.
 *
 * TIME. Clinic hours are hospital wall-clock times and render through
 * formatClinicalClock; days are hospital calendar days and render through
 * formatClinicalDateKey. Slots come from the server, computed in hospital time,
 * and are never worked out here. This screen says "slots", not "free slots":
 * bookings do not carry a slot yet.
 *
 * No patient data is read or shown here.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import {
  WEEKDAY_LABELS,
  addSessionException,
  createClinicSession,
  fetchAvailability,
  fetchClinicDoctors,
  fetchClinicSessions,
  fetchSessionExceptions,
  isFeatureDisabled,
  retireClinicSession,
  updateClinicSession,
  withdrawSessionException,
  type Availability,
  type ClinicDoctor,
  type ClinicSession,
  type SessionException,
  type Weekday,
} from '../../../api/schedule'
import { formatClinicalClock, formatClinicalDateKey } from '../../../lib/datetime'
import { SCHEDULE_MANAGE, hasPermission } from '../../../lib/roles'
import type { FrontDeskViewProps } from './viewRegistry'
import './ClinicSessionsView.css'

export const viewId = 'clinic-sessions'
export const viewLabel = 'Clinic timings'
export const viewOrder = 30
export const viewPermission = 'registration.read'

function errorText(err: unknown): string {
  if (err instanceof Error && err.message) return err.message
  return 'Something went wrong. Nothing was saved.'
}

function hours(s: { start_time: string; end_time: string }): string {
  return `${formatClinicalClock(s.start_time)} to ${formatClinicalClock(s.end_time)}`
}

function runs(s: ClinicSession): string {
  if (s.valid_from && s.valid_to) {
    return `${formatClinicalDateKey(s.valid_from)} to ${formatClinicalDateKey(s.valid_to)}`
  }
  if (s.valid_from) return `From ${formatClinicalDateKey(s.valid_from)}`
  if (s.valid_to) return `Until ${formatClinicalDateKey(s.valid_to)}`
  return 'Every week'
}

// ── The add and edit form ────────────────────────────────────────────────────

interface FormState {
  doctorId: string
  weekday: string
  start: string
  end: string
  slot: string
  capacity: string
  room: string
  from: string
  to: string
  notes: string
}

const EMPTY_FORM: FormState = {
  doctorId: '', weekday: '0', start: '09:00', end: '13:00', slot: '15', capacity: '1',
  room: '', from: '', to: '', notes: '',
}

function formFrom(s: ClinicSession): FormState {
  return {
    doctorId: s.doctor_id,
    weekday: String(s.weekday),
    start: s.start_time,
    end: s.end_time,
    slot: String(s.slot_minutes),
    capacity: String(s.capacity_per_slot),
    room: s.room ?? '',
    from: s.valid_from ?? '',
    to: s.valid_to ?? '',
    notes: s.notes ?? '',
  }
}

function SessionForm({
  doctors,
  editing,
  onSaved,
  onCancelEdit,
  onFeatureOff,
}: {
  doctors: ClinicDoctor[]
  editing: ClinicSession | null
  onSaved: () => void
  onCancelEdit: () => void
  onFeatureOff: () => void
}) {
  const [form, setForm] = useState<FormState>(() => (editing ? formFrom(editing) : EMPTY_FORM))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const set = (key: keyof FormState) => (
    e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>,
  ) => setForm(prev => ({ ...prev, [key]: e.target.value }))

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (busy) return
    if (!editing && !form.doctorId) {
      setError('Choose the doctor who holds this clinic.')
      return
    }
    setBusy(true)
    setError('')
    const fields = {
      weekday: Number(form.weekday) as Weekday,
      start_time: form.start,
      end_time: form.end,
      slot_minutes: Number(form.slot),
      capacity_per_slot: Number(form.capacity),
      room: form.room.trim() || null,
      valid_from: form.from || null,
      valid_to: form.to || null,
      notes: form.notes.trim() || null,
    }
    try {
      if (editing) {
        await updateClinicSession(editing.id, editing.row_version, fields)
      } else {
        await createClinicSession({ doctor_id: form.doctorId, ...fields })
        setForm(prev => ({ ...EMPTY_FORM, doctorId: prev.doctorId }))
      }
      onSaved()
    } catch (err) {
      if (isFeatureDisabled(err)) { onFeatureOff(); return }
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  const prefix = editing ? 'cs-edit' : 'cs-new'
  return (
    <form className="fd-card cs-form" onSubmit={submit} aria-labelledby={`${prefix}-title`}>
      <h3 className="fd-card__title" id={`${prefix}-title`}>
        {editing ? 'Change this clinic' : 'Add a clinic'}
      </h3>
      {editing ? (
        <p className="fd-hint cs-form__wide">
          {editing.doctor_name ?? editing.doctor_id}, {editing.department}. To move a clinic to
          another doctor, retire it and add a new one.
        </p>
      ) : (
        <div className="fd-field cs-form__wide">
          <label htmlFor={`${prefix}-doctor`}>Doctor</label>
          <select id={`${prefix}-doctor`} value={form.doctorId} onChange={set('doctorId')} required>
            <option value="">Select a doctor</option>
            {doctors.map(d => (
              <option key={d.id} value={d.id}>{d.name} ({d.department})</option>
            ))}
          </select>
        </div>
      )}
      <div className="fd-field">
        <label htmlFor={`${prefix}-weekday`}>Day</label>
        <select id={`${prefix}-weekday`} value={form.weekday} onChange={set('weekday')}>
          {WEEKDAY_LABELS.map((label, i) => <option key={label} value={String(i)}>{label}</option>)}
        </select>
      </div>
      <div className="fd-field">
        <label htmlFor={`${prefix}-start`}>Starts</label>
        <input id={`${prefix}-start`} type="time" value={form.start} onChange={set('start')} required />
      </div>
      <div className="fd-field">
        <label htmlFor={`${prefix}-end`}>Ends</label>
        <input id={`${prefix}-end`} type="time" value={form.end} onChange={set('end')} required />
      </div>
      <div className="fd-field">
        <label htmlFor={`${prefix}-slot`}>Slot length (minutes)</label>
        <input id={`${prefix}-slot`} type="number" min={5} max={240} step={5}
          value={form.slot} onChange={set('slot')} required />
      </div>
      <div className="fd-field">
        <label htmlFor={`${prefix}-capacity`}>Patients per slot</label>
        <input id={`${prefix}-capacity`} type="number" min={1} max={50}
          value={form.capacity} onChange={set('capacity')} required />
      </div>
      <div className="fd-field">
        <label htmlFor={`${prefix}-room`}>Room</label>
        <input id={`${prefix}-room`} type="text" value={form.room} onChange={set('room')}
          placeholder="For example, Room 4" />
      </div>
      <div className="fd-field">
        <label htmlFor={`${prefix}-from`}>First day (optional)</label>
        <input id={`${prefix}-from`} type="date" value={form.from} onChange={set('from')} />
      </div>
      <div className="fd-field">
        <label htmlFor={`${prefix}-to`}>Last day (optional)</label>
        <input id={`${prefix}-to`} type="date" value={form.to} onChange={set('to')} />
      </div>
      <div className="fd-field cs-form__wide">
        <label htmlFor={`${prefix}-notes`}>Notes</label>
        <input id={`${prefix}-notes`} type="text" value={form.notes} onChange={set('notes')} />
      </div>
      {error && <p className="fd-error cs-form__wide" role="alert">{error}</p>}
      <div className="cs-form__actions cs-form__wide">
        {editing && (
          <button type="button" className="fd-btn fd-btn--quiet" onClick={onCancelEdit} disabled={busy}>
            Cancel
          </button>
        )}
        <button type="submit" className="fd-btn fd-btn--primary" disabled={busy}>
          {busy ? 'Saving…' : editing ? 'Save changes' : 'Add clinic'}
        </button>
      </div>
    </form>
  )
}

// ── A clinic not running on a day ────────────────────────────────────────────

function ExceptionForm({
  doctors,
  sessions,
  onSaved,
  onFeatureOff,
}: {
  doctors: ClinicDoctor[]
  sessions: ClinicSession[]
  onSaved: () => void
  onFeatureOff: () => void
}) {
  const [doctorId, setDoctorId] = useState('')
  const [sessionId, setSessionId] = useState('')
  const [day, setDay] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const own = sessions.filter(s => s.doctor_id === doctorId)
  const chosenSession = own.some(s => s.id === sessionId) ? sessionId : ''

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (busy) return
    if (!doctorId || !day || !reason.trim()) {
      setError('Choose the doctor and the day, and say why the clinic is not running.')
      return
    }
    setBusy(true)
    setError('')
    try {
      await addSessionException({
        doctor_id: doctorId,
        session_id: chosenSession || undefined,
        exception_date: day,
        reason: reason.trim(),
      })
      setReason('')
      onSaved()
    } catch (err) {
      if (isFeatureDisabled(err)) { onFeatureOff(); return }
      setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="fd-card cs-form" onSubmit={submit} aria-labelledby="cs-off-title">
      <h3 className="fd-card__title" id="cs-off-title">Mark a clinic as not running</h3>
      <div className="fd-field">
        <label htmlFor="cs-off-doctor">Doctor</label>
        <select id="cs-off-doctor" value={doctorId} onChange={e => setDoctorId(e.target.value)}>
          <option value="">Select a doctor</option>
          {doctors.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}
        </select>
      </div>
      <div className="fd-field">
        <label htmlFor="cs-off-session">Clinic</label>
        <select id="cs-off-session" value={chosenSession} onChange={e => setSessionId(e.target.value)}
          disabled={!doctorId}>
          <option value="">Every clinic that day</option>
          {own.map(s => (
            <option key={s.id} value={s.id}>{s.weekday_name}, {hours(s)}</option>
          ))}
        </select>
      </div>
      <div className="fd-field">
        <label htmlFor="cs-off-day">Day</label>
        <input id="cs-off-day" type="date" value={day} onChange={e => setDay(e.target.value)} />
      </div>
      <div className="fd-field cs-form__wide">
        <label htmlFor="cs-off-reason">Reason</label>
        <input id="cs-off-reason" type="text" value={reason} onChange={e => setReason(e.target.value)}
          placeholder="For example, on leave" />
      </div>
      {error && <p className="fd-error cs-form__wide" role="alert">{error}</p>}
      <div className="cs-form__actions cs-form__wide">
        <button type="submit" className="fd-btn fd-btn--primary" disabled={busy}>
          {busy ? 'Saving…' : 'Mark as not running'}
        </button>
      </div>
    </form>
  )
}

// ── The view ─────────────────────────────────────────────────────────────────

interface ClinicSessionsViewProps extends FrontDeskViewProps {
  /** Tests only: whether this account may change the timetable. The shell
   *  mounts the view without it, which reads the client permission mirror. */
  canManage?: boolean
}

export default function ClinicSessionsView({
  onFeatureOff: reportFeatureOff,
  canManage = hasPermission(SCHEDULE_MANAGE),
}: ClinicSessionsViewProps) {
  /* Switched off while open: say so here as well as telling the panel, so this
     view never draws an empty timetable in the moment before it is removed. */
  const [switchedOff, setSwitchedOff] = useState(false)
  const onFeatureOff = useCallback(() => {
    setSwitchedOff(true)
    reportFeatureOff()
  }, [reportFeatureOff])
  const [doctors, setDoctors] = useState<ClinicDoctor[]>([])
  const [sessions, setSessions] = useState<ClinicSession[]>([])
  const [exceptions, setExceptions] = useState<SessionException[]>([])
  const [availability, setAvailability] = useState<Availability | null>(null)
  const [day, setDay] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [actionError, setActionError] = useState('')
  const [editing, setEditing] = useState<ClinicSession | null>(null)
  const [retiring, setRetiring] = useState<string>('')
  const [retireReason, setRetireReason] = useState('')
  const [busyId, setBusyId] = useState('')
  const live = useRef(true)

  useEffect(() => {
    live.current = true
    return () => { live.current = false }
  }, [])

  const load = useCallback(async (forDay: string) => {
    try {
      const [d, s, x, a] = await Promise.all([
        fetchClinicDoctors(),
        fetchClinicSessions(),
        fetchSessionExceptions(),
        fetchAvailability({ date: forDay || undefined }),
      ])
      if (!live.current) return
      setDoctors(d.doctors)
      setSessions(s.sessions)
      setExceptions(x.exceptions)
      setAvailability(a)
      setDay(a.from)
      setError('')
    } catch (err) {
      if (!live.current) return
      if (isFeatureDisabled(err)) { onFeatureOff(); return }
      setError(errorText(err))
    } finally {
      if (live.current) setLoading(false)
    }
  }, [onFeatureOff])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger: the loader sets state only after its requests answer.
  useEffect(() => { void load('') }, [load])

  const reload = useCallback(() => { void load(day) }, [load, day])

  const showDay = async (next: string) => {
    setDay(next)
    if (!next) return
    try {
      const a = await fetchAvailability({ date: next })
      if (live.current) { setAvailability(a); setError('') }
    } catch (err) {
      if (isFeatureDisabled(err)) { onFeatureOff(); return }
      if (live.current) setError(errorText(err))
    }
  }

  const retire = async (s: ClinicSession) => {
    if (!retireReason.trim()) {
      setActionError('Say why this clinic is being retired.')
      return
    }
    setBusyId(s.id)
    setActionError('')
    try {
      await retireClinicSession(s.id, retireReason.trim())
      setRetiring('')
      setRetireReason('')
      reload()
    } catch (err) {
      if (isFeatureDisabled(err)) { onFeatureOff(); return }
      setActionError(errorText(err))
    } finally {
      setBusyId('')
    }
  }

  const withdraw = async (x: SessionException) => {
    setBusyId(x.id)
    setActionError('')
    try {
      await withdrawSessionException(x.id, 'The clinic is running after all')
      reload()
    } catch (err) {
      if (isFeatureDisabled(err)) { onFeatureOff(); return }
      setActionError(errorText(err))
    } finally {
      setBusyId('')
    }
  }

  const sessionLabel = useMemo(() => {
    const byId = new Map(sessions.map(s => [s.id, s]))
    return (x: SessionException) => {
      if (!x.session_id) return 'Every clinic that day'
      const s = byId.get(x.session_id)
      return s ? hours(s) : 'One clinic'
    }
  }, [sessions])

  if (switchedOff) {
    return <p className="fd-empty">Clinic timings are switched off on this site.</p>
  }

  if (loading) return <p className="orb-panel-loading">Loading clinic timings…</p>

  if (error && !availability) {
    return (
      <div className="fd-queues">
        <p className="fd-error" role="alert">{error}</p>
        <div>
          <button type="button" className="fd-btn" onClick={reload}>Try again</button>
        </div>
      </div>
    )
  }

  const shownDay = availability?.days[0]

  return (
    <div className="cs-view">
      {error && <p className="fd-error" role="alert">{error}</p>}

      <section className="fd-card" aria-labelledby="cs-day-title">
        <div className="cs-day-head">
          <h3 className="fd-card__title" id="cs-day-title">
            {shownDay ? `${shownDay.weekday_name}, ${formatClinicalDateKey(shownDay.date)}` : 'Clinics'}
          </h3>
          <div className="fd-field cs-day-pick">
            <label htmlFor="cs-day">Show day</label>
            <input id="cs-day" type="date" value={day} onChange={e => { void showDay(e.target.value) }} />
          </div>
        </div>
        <p className="fd-hint">
          Times are hospital time. These are the slots each clinic offers; bookings are not
          counted against them yet.
        </p>
        {!shownDay || shownDay.sessions.length === 0 ? (
          <p className="fd-empty">No clinic is held on this day.</p>
        ) : (
          <ul className="cs-day-list">
            {shownDay.sessions.map(s => (
              <li key={s.session_id} className="cs-day-item">
                <div className="cs-day-item__who">
                  <span className="cs-strong">{s.doctor_name ?? s.doctor_id}</span>
                  <span className="cs-sub">
                    {s.department}{s.room ? `, ${s.room}` : ''}, {hours(s)}
                  </span>
                </div>
                {s.cancelled ? (
                  <p className="cs-off" role="note">
                    Not running: {s.cancel_reason || 'no reason recorded'}
                  </p>
                ) : (
                  <ol className="cs-slots" aria-label={`Slots for ${s.doctor_name ?? s.doctor_id}`}>
                    {s.slots.map(slot => (
                      <li key={slot.start} className={`cs-slot${slot.past ? ' cs-slot--past' : ''}`}>
                        {formatClinicalClock(slot.start)}
                        {slot.past && <span className="cs-visually-hidden"> (started)</span>}
                      </li>
                    ))}
                  </ol>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="fd-card" aria-labelledby="cs-week-title">
        <h3 className="fd-card__title" id="cs-week-title">Weekly clinics</h3>
        {actionError && <p className="fd-error" role="alert">{actionError}</p>}
        {sessions.length === 0 ? (
          <p className="fd-empty">
            No clinic timings have been set up yet.
            {canManage ? ' Add the first one below.' : ' The administrator sets them up.'}
          </p>
        ) : (
          <div className="fd-table-scroll">
            <table className="fd-table">
              <thead>
                <tr>
                  <th scope="col">Doctor</th>
                  <th scope="col">Day</th>
                  <th scope="col">Hours</th>
                  <th scope="col">Slots</th>
                  <th scope="col">Room</th>
                  <th scope="col">Runs</th>
                  {canManage && <th scope="col">Action</th>}
                </tr>
              </thead>
              <tbody>
                {sessions.map(s => (
                  <tr key={s.id}>
                    <td>
                      <span className="cs-strong">{s.doctor_name ?? s.doctor_id}</span>
                      <span className="cs-sub">{s.department}</span>
                      {s.doctor_active === false && (
                        <span className="cs-warn">Account deactivated, no slots offered</span>
                      )}
                    </td>
                    <td className="fd-nowrap">{s.weekday_name}</td>
                    <td className="fd-nowrap">{hours(s)}</td>
                    <td className="fd-nowrap">
                      {s.slot_count} {s.slot_count === 1 ? 'slot' : 'slots'} of {s.slot_minutes} min
                      {s.capacity_per_slot > 1 ? `, ${s.capacity_per_slot} per slot` : ''}
                    </td>
                    <td>{s.room || 'Not set'}</td>
                    <td className="fd-nowrap">{runs(s)}</td>
                    {canManage && (
                      <td className="fd-actions-cell">
                        {retiring === s.id ? (
                          <div className="cs-retire">
                            <label className="cs-visually-hidden" htmlFor={`cs-retire-${s.id}`}>
                              Reason for retiring this clinic
                            </label>
                            <input
                              id={`cs-retire-${s.id}`}
                              className="cs-retire__reason"
                              type="text"
                              value={retireReason}
                              onChange={e => setRetireReason(e.target.value)}
                              placeholder="Why is it stopping?"
                            />
                            <button type="button" className="fd-btn fd-btn--primary"
                              disabled={busyId === s.id} onClick={() => { void retire(s) }}>
                              Retire clinic
                            </button>
                            <button type="button" className="fd-btn fd-btn--quiet"
                              onClick={() => { setRetiring(''); setRetireReason(''); setActionError('') }}>
                              Keep
                            </button>
                          </div>
                        ) : (
                          <div className="fd-actions">
                            <button type="button" className="fd-btn"
                              onClick={() => { setEditing(s); setRetiring('') }}>
                              Change
                            </button>
                            <button type="button" className="fd-btn fd-btn--quiet"
                              onClick={() => { setRetiring(s.id); setRetireReason(''); setEditing(null) }}>
                              Retire
                            </button>
                          </div>
                        )}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="fd-card" aria-labelledby="cs-off-list-title">
        <h3 className="fd-card__title" id="cs-off-list-title">Days not running, next 60 days</h3>
        {exceptions.length === 0 ? (
          <p className="fd-empty">Every clinic runs as usual.</p>
        ) : (
          <ul className="cs-off-list">
            {exceptions.map(x => (
              <li key={x.id} className="cs-off-item">
                <span className="cs-strong">{formatClinicalDateKey(x.exception_date)}</span>
                <span>{x.doctor_name ?? x.doctor_id}, {sessionLabel(x)}</span>
                <span className="cs-sub">{x.reason}</span>
                {canManage && (
                  <button type="button" className="fd-btn fd-btn--quiet" disabled={busyId === x.id}
                    onClick={() => { void withdraw(x) }}>
                    Running after all
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      {canManage && (
        <>
          <SessionForm
            key={editing ? `edit-${editing.id}-${editing.row_version}` : 'new'}
            doctors={doctors}
            editing={editing}
            onSaved={() => { setEditing(null); reload() }}
            onCancelEdit={() => setEditing(null)}
            onFeatureOff={onFeatureOff}
          />
          <ExceptionForm
            doctors={doctors}
            sessions={sessions}
            onSaved={reload}
            onFeatureOff={onFeatureOff}
          />
        </>
      )}
    </div>
  )
}
