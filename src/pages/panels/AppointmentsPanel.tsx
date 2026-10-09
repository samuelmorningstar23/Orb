import { useState, useEffect, useCallback, useRef } from 'react'
import { buildHeaders } from '../../api/client'
import { sexShort } from '../../lib/demographics'
import { formatClinicalClock, formatClinicalDate, localDateKey, parseUTC } from '../../lib/datetime'
import { newId } from '../../lib/id'
import { hasPermission, PATIENT_WRITE } from '../../lib/roles'
import { useClinicalSwitches } from '../../shellConfig'

const API = ''

async function apiFetch(path: string) {
  const res = await fetch(`${API}${path}`, {
    // Cookie session (buildHeaders adds a Bearer only if a token is persisted).
    credentials: 'include',
    headers: buildHeaders('GET'),
  })
  if (!res.ok) throw new Error(`API ${res.status}`)
  return res.json()
}

/** One row of the `appointments` table, as /api/forecast/appointments sends it.
 *  The server used to pad this list with a date-seeded random schedule that
 *  nobody booked; it now returns bookings only, and the panel shows only rows
 *  that say so (a stale cached or recorded response cannot slip one back in). */
interface Appointment {
  id?: string
  date: string            // hospital-local YYYY-MM-DD
  time: string            // HH:MM
  patient_id: string
  patient_name: string
  age: number | null
  sex: string | null
  ward: string | null
  bed: string | null
  diagnosis: string | null
  type: string
  /** One of APPOINTMENT_TYPES; an old free-text type arrives as 'other'. */
  type_key?: string
  /** The label to print: the list's word, or an old row's own text. */
  type_label?: string
  reason: string | null
  origin?: 'staff' | 'patient'
  department?: string | null
  /** The clinic doctor a slot booking is with (opd-16); null for a booking
   *  with no doctor, and absent from an older server. */
  doctor_id?: string | null
  doctor_name?: string | null
  /** The NEWS2 band of the patient's latest vitals (the Vigil board's rule),
   *  or null when nothing is recorded. */
  acuity: 'critical' | 'elevated' | 'stable' | null
  /** The NEWS2 behind that band, or null. */
  news2?: number | null
  /** False when the set was incomplete, so the number is a minimum. */
  news2_complete?: boolean | null
  source?: string
}

/** The four types a clinician can book (server/services/appointment_types.py). */
const APPOINTMENT_TYPES: { key: string; label: string }[] = [
  { key: 'follow_up', label: 'Follow-up' },
  { key: 'medication_review', label: 'Medication review' },
  { key: 'post_discharge_check', label: 'Post-discharge check' },
  { key: 'other', label: 'Other' },
]

/** A discharged patient stays bookable this long after going home. */
const RECENT_DISCHARGE_DAYS = 30

interface BookablePatient { id: string; name: string; bed?: string | null; discharged_at?: string | null }

/** "Critical 9", the band word with the NEWS2 beside it; "≥" when the set was
 *  incomplete. "Not scored" when nothing is recorded, never "Stable". */
function bandText(a: Pick<Appointment, 'acuity' | 'news2' | 'news2_complete'>): string {
  if (!a.acuity || !(a.acuity in ACUITY_TEXT)) return 'Not scored'
  const word = a.acuity.charAt(0).toUpperCase() + a.acuity.slice(1)
  if (typeof a.news2 !== 'number') return word
  return `${word} ${a.news2_complete === false ? '≥' : ''}${a.news2}`
}

const FIELD_STYLE: React.CSSProperties = {
  display: 'flex', flexDirection: 'column', gap: 4, fontSize: 'var(--text-xs)', color: 'var(--text-secondary)',
}
const INPUT_STYLE: React.CSSProperties = {
  padding: '8px 10px', borderRadius: 8, border: '1px solid var(--border-glass)',
  background: 'var(--bg-surface)', color: 'var(--text-primary)', fontSize: '0.85rem', fontFamily: 'inherit',
}

/* ── Booking form ──
   POST /api/appointments had no caller in the app: a slot could only be
   booked by the patient asking from the portal. Staff now book here, for a
   patient on the ward or one discharged in the last 30 days, with one of the
   four types. */
function BookingForm({ onBooked, onClose }: { onBooked: () => void; onClose: () => void }) {
  const [patients, setPatients] = useState<{ admitted: BookablePatient[]; discharged: BookablePatient[] } | null>(null)
  const [loadError, setLoadError] = useState('')
  const [patientId, setPatientId] = useState('')
  const [type, setType] = useState('follow_up')
  const [date, setDate] = useState(localDateKey())
  const [time, setTime] = useState('10:00')
  const [reason, setReason] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  // One request key per booking asked for (the patient, day and time, the
  // fields the server binds a replay to): a retry of the same booking replays
  // it, and a changed booking is a new request, never a replay of the old one.
  const idem = useRef<{ request: string; key: string } | null>(null)

  useEffect(() => {
    let live = true
    Promise.all([
      apiFetch('/api/vigil/patients?limit=500'),
      apiFetch('/api/vigil/discharged?limit=200'),
    ]).then(([admitted, discharged]) => {
      if (!live) return
      const cutoff = Date.now() - RECENT_DISCHARGE_DAYS * 86_400_000
      const recent = (Array.isArray(discharged) ? discharged : []).filter((p: BookablePatient) => {
        if (!p.discharged_at) return false
        const t = parseUTC(String(p.discharged_at)).getTime()
        return Number.isFinite(t) && t >= cutoff
      })
      setPatients({ admitted: Array.isArray(admitted) ? admitted : [], discharged: recent })
    }).catch(() => { if (live) setLoadError('Could not load the patient list.') })
    return () => { live = false }
  }, [])

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (!patientId) { setError('Choose a patient.'); return }
    const request = `${patientId}|${date}|${time}`
    if (idem.current?.request !== request) idem.current = { request, key: newId() }
    const idemKey = idem.current.key
    setSaving(true)
    setError('')
    try {
      const res = await fetch('/api/appointments', {
        method: 'POST',
        credentials: 'include',
        headers: buildHeaders('POST', { 'Idempotency-Key': idemKey }),
        body: JSON.stringify({
          patient_id: patientId, appointment_type: type,
          scheduled_date: date, scheduled_time: time, reason: reason.trim() || null,
        }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        const detail = typeof body?.detail === 'string' ? body.detail : body?.detail?.message
        throw new Error(detail || `Could not book (error ${res.status}).`)
      }
      onBooked()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not book.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <form
      aria-label="Book an appointment"
      onSubmit={submit}
      className="glass-card"
      style={{ padding: '14px 16px', marginBottom: 20, display: 'grid', gap: 12,
               gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', alignItems: 'end' }}
    >
      <label style={FIELD_STYLE}>
        Patient
        <select style={INPUT_STYLE} value={patientId} onChange={e => setPatientId(e.target.value)} required>
          <option value="">{patients ? 'Choose a patient' : 'Loading patients...'}</option>
          {patients && patients.admitted.length > 0 && (
            <optgroup label="On the ward">
              {patients.admitted.map(p => (
                <option key={p.id} value={p.id}>{p.name}{p.bed ? `, bed ${p.bed}` : ''}</option>
              ))}
            </optgroup>
          )}
          {patients && patients.discharged.length > 0 && (
            <optgroup label={`Discharged in the last ${RECENT_DISCHARGE_DAYS} days`}>
              {patients.discharged.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
            </optgroup>
          )}
        </select>
      </label>
      <label style={FIELD_STYLE}>
        Type
        <select style={INPUT_STYLE} value={type} onChange={e => setType(e.target.value)}>
          {APPOINTMENT_TYPES.map(t => <option key={t.key} value={t.key}>{t.label}</option>)}
        </select>
      </label>
      <label style={FIELD_STYLE}>
        Date
        <input style={INPUT_STYLE} type="date" value={date} min={localDateKey()} onChange={e => setDate(e.target.value)} required />
      </label>
      <label style={FIELD_STYLE}>
        Time
        <input style={INPUT_STYLE} type="time" value={time} onChange={e => setTime(e.target.value)} required />
      </label>
      <label style={{ ...FIELD_STYLE, gridColumn: '1 / -1' }}>
        Reason
        <input style={INPUT_STYLE} type="text" value={reason} maxLength={2000}
               placeholder="What the visit is for" onChange={e => setReason(e.target.value)} />
      </label>
      {(error || loadError) && (
        <p role="alert" style={{ gridColumn: '1 / -1', margin: 0, fontSize: '0.85rem', color: 'var(--status-danger-text)' }}>
          {error || loadError}
        </p>
      )}
      <div style={{ gridColumn: '1 / -1', display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
        <button type="button" className="btn-ghost" onClick={onClose}>Cancel</button>
        <button type="submit" className="btn-primary" disabled={saving}>{saving ? 'Booking...' : 'Book appointment'}</button>
      </div>
    </form>
  )
}

/** A YYYY-MM-DD key as a LOCAL calendar date. new Date('2026-10-03') is UTC
 *  midnight, which renders as the previous day anywhere west of Greenwich. */
function keyToLocalDate(key: string): Date {
  const [y, m, d] = key.split('-').map(Number)
  return new Date(y, (m || 1) - 1, d || 1)
}

const ACUITY_TEXT: Record<string, string> = {
  critical: 'var(--status-danger-text)',
  elevated: 'var(--status-warn-text)',
  stable: 'var(--status-ok-text)',
}
const ACUITY_TINT: Record<string, string> = {
  critical: 'var(--status-danger)',
  elevated: 'var(--status-warn)',
  stable: 'var(--status-ok)',
}

// ── Metric Card ──
function MetricCard({ label, value, unit, accent, subtitle }: {
  label: string; value: string | number; unit?: string; accent: string; subtitle?: string
}) {
  return (
    <div className="forecast-metric-card" style={{ '--accent': accent } as React.CSSProperties}>
      <span className="forecast-metric-label">{label}</span>
      <div className="forecast-metric-value">
        <span className="metric-mono" style={{ color: accent, fontSize: '1.8rem' }}>{value}</span>
        {unit && <span className="forecast-metric-unit">{unit}</span>}
      </div>
      {subtitle && <span className="forecast-metric-subtitle">{subtitle}</span>}
    </div>
  )
}

export default function AppointmentsPanel() {
  const [appointments, setAppointments] = useState<Appointment[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [booking, setBooking] = useState(false)
  const canBook = hasPermission(PATIENT_WRITE)
  // Scoring switched off (the Orb Ward Record): the server sends no band, so
  // no badge and no critical count are drawn, and a day is listed by time.
  const { scoringEnabled } = useClinicalSwitches()

  const fetchAppointments = useCallback(async () => {
    try {
      setLoading(true)
      const appts: Appointment[] = await apiFetch('/api/forecast/appointments')
      setAppointments(Array.isArray(appts) ? appts.filter(a => a.source === 'booked') : [])
      setError('')
    } catch (err) {
      console.error("Failed to fetch appointments:", err)
      setError("Failed to load appointments. Please check connection.")
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    fetchAppointments()
  }, [fetchAppointments])

  if (loading) return <div className="orb-panel-loading">Loading appointments...</div>

  if (error) {
    return (
      <div className="orb-panel-error" role="alert">
        <p>{error}</p>
        <button type="button" onClick={fetchAppointments}>Retry</button>
      </div>
    )
  }

  // Group by date, keeping the server's order: date, then the sickest patient
  // first (highest NEWS2 of the latest vitals), then time. With scoring
  // switched off the server sends no score, so a day is in time order.
  const grouped = appointments.reduce<Record<string, Appointment[]>>((acc, a) => {
    (acc[a.date] ||= []).push(a)
    return acc
  }, {})
  const dayCount = Object.keys(grouped).length
  const today = localDateKey()
  // Patients, not rows: one critical patient booked on three days is one
  // patient needing extra attention, not three.
  const criticalPatients = new Set(
    appointments.filter(a => a.acuity === 'critical').map(a => a.patient_id),
  ).size
  const patientRequests = appointments.filter(a => a.origin === 'patient').length
  // The list is the next 7 days already, so every row counts.
  const postDischargeChecks = appointments.filter(a => a.type_key === 'post_discharge_check').length

  return (
    <div className="forecast-section animate-fade-in">
      <div style={{ marginBottom: 24 }}>
        <h2 className="detail__section-title" style={{ fontSize: '1.4rem' }}>Booked appointments</h2>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
          <p style={{ fontSize: '0.9rem', color: 'var(--text-secondary)', margin: 0 }}>Appointments booked for the next 7 days.</p>
          {/* Booking needs patient.write on the server. A nurse or an
              administrator can read this list but would fill the form in only
              to be refused, so the button is not offered to them. */}
          {!booking && canBook && (
            <button type="button" className="btn-primary" onClick={() => setBooking(true)}>Book appointment</button>
          )}
        </div>
      </div>

      {booking && (
        <BookingForm
          onClose={() => setBooking(false)}
          onBooked={() => { setBooking(false); fetchAppointments() }}
        />
      )}

      {/* Summary strip */}
      <div className="forecast-kpi-strip" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: '16px', marginBottom: '24px' }}>
        <MetricCard
          label="Today"
          value={appointments.filter(a => a.date === today).length}
          accent="var(--accent-text)"
          subtitle="Booked for today"
        />
        <MetricCard
          label="Next 7 days"
          value={appointments.length}
          accent="var(--text-primary)"
          subtitle={`${dayCount} ${dayCount === 1 ? 'day' : 'days'} with bookings`}
        />
        {scoringEnabled && (
          <MetricCard
            label="Critical patients"
            value={criticalPatients}
            accent="var(--status-danger-text)"
            subtitle="Latest NEWS2 critical"
          />
        )}
        <MetricCard
          label="Post-discharge checks"
          value={postDischargeChecks}
          accent="var(--text-primary)"
          subtitle="Due in the next 7 days"
        />
        <MetricCard
          label="Patient requests"
          value={patientRequests}
          accent="var(--text-primary)"
          subtitle="Time still to confirm"
        />
      </div>

      {appointments.length === 0 ? (
        <div className="orb-state orb-state--empty">
          <div className="orb-state__content">
            <span className="orb-state__title">No appointments booked for the next 7 days.</span>
          </div>
        </div>
      ) : (
      /* Day columns wrap instead of scrolling sideways: six fixed 240px
         columns overflowed a laptop screen behind an overlay scrollbar that
         never showed, so later days were simply missing to the reader. */
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))', gap: '12px', alignItems: 'start' }}>
      {Object.entries(grouped).map(([date, items]) => {
        const isToday = date === today
        const day = keyToLocalDate(date)
        const weekday = day.toLocaleDateString('en-GB', { weekday: 'short' })
        const label = formatClinicalDate(day)
        return (
          <div key={date} className="glass-card" style={{ padding: '12px 14px', borderLeft: isToday ? '3px solid var(--accent-amber)' : '3px solid var(--border-glass)', display: 'flex', flexDirection: 'column' }}>
            {/* Day header */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12, flexShrink: 0 }}>
              {/* The weekday, not the day of the month: a column of bare
                  numbers read as a sequence count. */}
              <div style={{
                minWidth: 40, height: 32, padding: '0 6px', borderRadius: 8,
                background: isToday ? 'var(--btn-gradient)' : 'var(--bg-glass)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                color: isToday ? 'var(--btn-primary-text)' : 'var(--text-secondary)',
                fontSize: 'var(--text-xs)', fontWeight: 700,
              }}>
                {weekday}
              </div>
              <div style={{ minWidth: 0 }}>
                {/* The badge sits OUTSIDE the ellipsized span: inside it, the
                    badge was the tail that got clipped to "TOD…" (§13.7). */}
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <span style={{ fontSize: '0.85rem', fontWeight: 600, color: 'var(--text-primary)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', minWidth: 0 }} title={label}>{label}</span>
                  {isToday && <span style={{ flexShrink: 0, fontSize: 'var(--text-xs)', padding: '1px 6px', borderRadius: 999, background: 'var(--accent-glow)', color: 'var(--accent-text)', fontWeight: 600 }}>Today</span>}
                </div>
                <div style={{ fontSize: 'var(--text-xs)', color: 'var(--text-muted)' }}>
                  {items.length} {items.length === 1 ? 'appointment' : 'appointments'}
                </div>
              </div>
            </div>

            {/* Appointment cards */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              {items.map((appt, i) => {
                const scored = appt.acuity !== null && appt.acuity in ACUITY_TEXT
                const acText = scored ? ACUITY_TEXT[appt.acuity as string] : 'var(--text-muted)'
                const acTint = scored ? ACUITY_TINT[appt.acuity as string] : 'var(--text-muted)'
                return (
                  <div key={appt.id ?? `${appt.patient_id}-${i}`} style={{
                    display: 'grid',
                    gridTemplateColumns: '76px 1fr',
                    gap: 10,
                    padding: '8px 10px',
                    borderRadius: 8,
                    background: 'var(--bg-glass)',
                    border: '1px solid var(--border-glass)',
                  }}>
                    {/* Time */}
                    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 2 }}>
                      <span className="metric-mono" style={{ fontSize: '0.85rem', fontWeight: 700, color: 'var(--text-primary)' }}>{formatClinicalClock(appt.time)}</span>
                      {scoringEnabled && (
                        <span className="appt-band" style={{
                          fontSize: 'var(--text-xs)', fontWeight: 600, whiteSpace: 'nowrap',
                          padding: '1px 4px', borderRadius: 4,
                          background: `color-mix(in srgb, ${acTint} 12%, transparent)`, color: acText,
                        }} title={typeof appt.news2 === 'number' ? `NEWS2 ${appt.news2} (${appt.acuity})` : 'NEWS2 not recorded'}>
                          {bandText(appt)}
                        </span>
                      )}
                    </div>

                    {/* Patient info */}
                    <div style={{ minWidth: 0, display: 'flex', flexDirection: 'column', justifyContent: 'center' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 2 }}>
                        <span style={{ fontSize: '0.8rem', fontWeight: 600, color: 'var(--text-primary)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={appt.patient_name}>
                          {appt.patient_name}
                        </span>
                        <span style={{ fontSize: 'var(--text-xs)', color: 'var(--text-muted)', flexShrink: 0 }}>
                          {appt.age ?? ''}{sexShort(appt.sex, '')}
                        </span>
                      </div>
                      <div style={{ fontSize: 'var(--text-xs)', color: 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={appt.type_label ?? appt.type}>
                        <strong style={{ color: 'var(--text-primary)', fontWeight: 500 }}>{appt.type_label ?? appt.type}</strong>
                      </div>
                      {/* The clinic doctor a slot booking is with (opd-16). */}
                      {appt.doctor_name && (
                        <div className="appt-doctor" style={{ fontSize: 'var(--text-xs)', color: 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={`With ${appt.doctor_name}`}>
                          With {appt.doctor_name}
                        </div>
                      )}
                      {appt.reason && (
                        <div style={{ fontSize: 'var(--text-xs)', color: 'var(--text-muted)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} title={appt.reason}>
                          {appt.reason}
                        </div>
                      )}
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        )
      })}
      </div>
      )}
    </div>
  )
}
