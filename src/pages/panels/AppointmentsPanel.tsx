import { useState, useEffect, useCallback } from 'react'
import { buildHeaders } from '../../api/client'
import { sexShort } from '../../lib/demographics'
import { formatClinicalClock, formatClinicalDate, localDateKey } from '../../lib/datetime'

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
  reason: string | null
  origin?: 'staff' | 'patient'
  department?: string | null
  /** The latest real risk score, or null when the patient has never been scored. */
  acuity: 'critical' | 'elevated' | 'stable' | null
  source?: string
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

  // Group by date, keeping the server's date-then-time order.
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

  return (
    <div className="forecast-section animate-fade-in">
      <div style={{ marginBottom: 24 }}>
        <h2 className="detail__section-title" style={{ fontSize: '1.4rem' }}>Booked appointments</h2>
        <p style={{ fontSize: '0.9rem', color: 'var(--text-secondary)' }}>Appointments booked for the next 7 days.</p>
      </div>

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
        <MetricCard
          label="Critical patients"
          value={criticalPatients}
          accent="var(--status-danger-text)"
          subtitle="Latest risk score critical"
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
                    gridTemplateColumns: '64px 1fr',
                    gap: 10,
                    padding: '8px 10px',
                    borderRadius: 8,
                    background: 'var(--bg-glass)',
                    border: '1px solid var(--border-glass)',
                  }}>
                    {/* Time */}
                    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 2 }}>
                      <span className="metric-mono" style={{ fontSize: '0.85rem', fontWeight: 700, color: 'var(--text-primary)' }}>{formatClinicalClock(appt.time)}</span>
                      <span style={{
                        fontSize: 'var(--text-xs)', fontWeight: 600, whiteSpace: 'nowrap',
                        padding: '1px 4px', borderRadius: 4,
                        background: `color-mix(in srgb, ${acTint} 12%, transparent)`, color: acText,
                      }}>
                        {scored ? appt.acuity!.charAt(0).toUpperCase() + appt.acuity!.slice(1) : 'Not scored'}
                      </span>
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
                      <div style={{ fontSize: 'var(--text-xs)', color: 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={appt.type}>
                        <strong style={{ color: 'var(--text-primary)', fontWeight: 500 }}>{appt.type}</strong>
                      </div>
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
