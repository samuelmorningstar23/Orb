import { useState, useEffect, useMemo, useCallback } from 'react'
import { useAuth } from '../App'
import {
  getBridgeStatus,
  askBridge,
  getBridgeReports,
  apiFetch,
  type BridgeDischargeView,
  type BridgeMedication,
  type BridgeReports,
  type BridgeStatus,
} from '../api/client'
import OrbLogo from '../components/OrbLogo'
import './PatientShell.css'
import { CareTeamCard } from '../components/bridge/CareTeamCard'
import { VitalsTrendsCard } from '../components/bridge/VitalsTrendsCard'
import { useFocusTrap } from '../hooks/useFocusTrap'
import { formatClinicalDate, formatClinicalDateTime, parseUTC } from '../lib/datetime'

/* ── helpers ── */

/**
 * Dialog wrapper for the patient portal's modals.
 *
 * These are read by patients and family members, often on a bedside tablet or
 * with assistive tech. Without a trap, Escape did nothing and Tab walked focus
 * out of the modal onto portal controls hidden behind the scrim, which could
 * then be activated blind. useFocusTrap supplies Escape, Tab containment,
 * focus restore and the body scroll lock; role/aria-modal announce the
 * boundary.
 */
function PortalModal({
  overlayClass, modalClass, style, labelledBy, onClose, children,
}: {
  overlayClass: string
  modalClass: string
  style?: React.CSSProperties
  labelledBy: string
  onClose: () => void
  children: React.ReactNode
}) {
  const ref = useFocusTrap<HTMLDivElement>(onClose)
  return (
    <div className={overlayClass} onClick={e => { if (e.target === e.currentTarget) onClose() }}>
      <div
        ref={ref}
        className={modalClass}
        style={style}
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        onClick={e => e.stopPropagation()}
      >
        {children}
      </div>
    </div>
  )
}
const DAYS = ['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa']
const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

/** "9:00 AM" → "09:00" (24h, what the API expects). */
function to24h(slot: string): string {
  const m = slot.match(/^(\d+):(\d+)\s*(AM|PM)$/i)
  if (!m) return slot
  let h = parseInt(m[1], 10)
  const mer = m[3].toUpperCase()
  if (mer === 'PM' && h !== 12) h += 12
  if (mer === 'AM' && h === 12) h = 0
  return `${String(h).padStart(2, '0')}:${m[2]}`
}

/** "14:00" → "2:00 PM" for display. */
function fmt12h(time: string): string {
  const [hStr, min] = (time || '').split(':')
  let h = parseInt(hStr, 10)
  if (Number.isNaN(h)) return time
  const mer = h >= 12 ? 'PM' : 'AM'
  h = h % 12
  if (h === 0) h = 12
  return `${h}:${min} ${mer}`
}

/** Local-date → "YYYY-MM-DD" (avoids the UTC shift from toISOString). */
function toISODate(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

/** "2026-07-10" → "Jul 10". */
function fmtApptDate(iso: string): string {
  const [, mo, d] = (iso || '').split('-').map(Number)
  if (!mo || !d) return iso
  return `${MONTHS[mo - 1]} ${d}`
}
const TIME_SLOTS = ['9:00 AM', '10:00 AM', '11:00 AM', '2:00 PM', '3:00 PM', '4:00 PM']
// `name` is what the request stores in its department, spelled out so the
// staff appointment list does not read "Cardio".
const DEPARTMENTS = [
  { id: 'primary', label: 'Primary', name: 'Primary care', icon: '🩺' },
  { id: 'cardio', label: 'Cardio', name: 'Cardiology', icon: '❤️' },
  { id: 'neuro', label: 'Neuro', name: 'Neurology', icon: '🧠' },
  { id: 'ortho', label: 'Ortho', name: 'Orthopaedics', icon: '🦴' },
  { id: 'lab', label: 'Lab', name: 'Laboratory', icon: '🧪' },
]

/** A Lab History row as /api/bridge/reports sends it: a charted lab result
 *  (title, content, type_label) or an imaging report (image_type, analysis). */
interface PortalReport {
  created_at?: string
  title?: string
  content?: string
  type_label?: string
  image_type?: string | null
  clinical_context?: string | null
  analysis?: string | null
  /** 'simulated' for a simulator's lab result, labelled on screen. */
  source?: string | null
}

/** What the detail modal opens: a medication, a document or lab result, an
 *  imaging report, or the discharge summary. */
type ReportDetail =
  | { type: 'med'; data: BridgeMedication }
  | { type: 'doc' | 'lab'; data: PortalReport }
  | { type: 'discharge'; data: BridgeDischargeView }

/** An appointments row as /appointments/mine sends it
 *  (appointments_routes._serialize). */
interface PortalAppointment {
  id: string
  patient_id: string
  booked_by_user_id: string | null
  scheduled_date: string
  scheduled_time: string
  appointment_type: string
  /** The type in plain words (appointment_types.type_label). */
  appointment_type_label?: string
  reason: string | null
  status: string
  created_at: string
  /** 'patient' when the patient asked for it and nobody has agreed the slot. */
  origin: string
  department: string | null
}

/** POST /bridge/appointments: the row that exists after the ask, which is the
 *  care team's own booking when the slot was already in the diary. */
interface AppointmentRequestResult {
  id: string
  status: string
  requested_date: string
  requested_time: string
  department: string | null
  requested_by_patient: boolean
  time_chosen_by_patient: boolean
  message: string
}

/** Returns the 7 dates of the week containing `anchorDate` */
function getWeekDates(anchorDate: Date): Date[] {
  const start = new Date(anchorDate)
  start.setDate(start.getDate() - start.getDay()) // back to Sunday
  return Array.from({ length: 7 }, (_, i) => {
    const d = new Date(start)
    d.setDate(start.getDate() + i)
    return d
  })
}

/** The month or months a week covers: "Oct 2026", "Sep to Oct 2026" or
 *  "Dec 2026 to Jan 2027". Taken from Sunday alone, the 27 Sep to 3 Oct week
 *  read "Sep 2026" while the patient was choosing a day in October. */
function weekMonthLabel(dates: Date[]): string {
  const first = dates[0]
  const last = dates[dates.length - 1]
  if (first.getFullYear() !== last.getFullYear()) {
    return `${MONTHS[first.getMonth()]} ${first.getFullYear()} to ${MONTHS[last.getMonth()]} ${last.getFullYear()}`
  }
  if (first.getMonth() !== last.getMonth()) {
    return `${MONTHS[first.getMonth()]} to ${MONTHS[last.getMonth()]} ${last.getFullYear()}`
  }
  return `${MONTHS[first.getMonth()]} ${first.getFullYear()}`
}

/** True when `slot` ("9:00 AM") on `date` has already started at `now`. The
 *  server refuses a time earlier today, so the picker does not offer one. */
function slotHasPassed(date: Date, slot: string, now: Date): boolean {
  const [h, m] = to24h(slot).split(':').map(Number)
  return new Date(date.getFullYear(), date.getMonth(), date.getDate(), h, m) <= now
}

export default function PatientShell() {
  const { user, logout } = useAuth()
  const [status, setStatus] = useState<BridgeStatus | null>(null)
  const [question, setQuestion] = useState('')
  const [answer, setAnswer] = useState('')
  const [asking, setAsking] = useState(false)
  const [loading, setLoading] = useState(true)

  /* ── Reports state ── */
  const [reports, setReports] = useState<BridgeReports | null>(null)
  const [reportsLoading, setReportsLoading] = useState(false)
  const [selectedReportDetail, setSelectedReportDetail] = useState<ReportDetail | null>(null)
  const [medsModalOpen, setMedsModalOpen] = useState(false)
  const [docsModalOpen, setDocsModalOpen] = useState(false)
  const [labsModalOpen, setLabsModalOpen] = useState(false)
  const [dischargeModalOpen, setDischargeModalOpen] = useState(false)
  const [closingModal, setClosingModal] = useState<string | null>(null)

  const closeModal = useCallback((setter: (v: boolean) => void, name: string) => {
    setClosingModal(name)
    setTimeout(() => { setter(false); setClosingModal(null) }, 250)
  }, [])
  const closeDetail = useCallback(() => {
    setClosingModal('detail')
    setTimeout(() => { setSelectedReportDetail(null); setClosingModal(null) }, 250)
  }, [])

  /* ── Appointment Scheduler state ── */
  const today = useMemo(() => new Date(), [])
  const [weekAnchor, setWeekAnchor] = useState(() => new Date())
  const [selectedDate, setSelectedDate] = useState<Date | null>(null)
  const [selectedSlot, setSelectedSlot] = useState<string | null>(null)
  const [selectedDept, setSelectedDept] = useState<string | null>(null)
  const [bookingState, setBookingState] = useState<'idle' | 'confirming' | 'booked'>('idle')
  const [note, setNote] = useState('')
  const [bookError, setBookError] = useState('')
  const [confirmedAppt, setConfirmedAppt] = useState<AppointmentRequestResult | null>(null)
  const [upcoming, setUpcoming] = useState<PortalAppointment[]>([])

  /* ── FHIR record download state ── */
  const [fhirError, setFhirError] = useState('')
  const [fhirLoading, setFhirLoading] = useState(false)

  const weekDates = useMemo(() => getWeekDates(weekAnchor), [weekAnchor])

  const isDateDisabled = (d: Date) => {
    const todayMidnight = new Date(today.getFullYear(), today.getMonth(), today.getDate())
    return d < todayMidnight || d.getDay() === 0
  }

  const isSameDay = (a: Date, b: Date) =>
    a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()

  const handleWeekNav = (dir: number) => {
    setWeekAnchor(prev => {
      const next = new Date(prev)
      next.setDate(next.getDate() + dir * 7)
      return next
    })
  }

  const loadAppointments = useCallback(async () => {
    try {
      const data = await apiFetch<{ appointments: PortalAppointment[] }>('/appointments/mine')
      setUpcoming(data.appointments || [])
    } catch (err) {
      console.error('Failed to load appointments:', err)
    }
  }, [])

  const handleBook = async () => {
    if (!canBook || bookingState === 'confirming') return
    setBookingState('confirming')
    setBookError('')
    try {
      const dept = DEPARTMENTS.find(d => d.id === selectedDept)
      // The patient's route, which stores an ASK. The staff booking route saved
      // this as a booking the care team had made, and the card said "Confirmed".
      // A repeat tap returns the same row (the server dedupes by slot).
      const appt = await apiFetch<AppointmentRequestResult>('/bridge/appointments', {
        method: 'POST',
        body: JSON.stringify({
          requested_date: toISODate(selectedDate as Date),
          requested_time: to24h(selectedSlot as string),
          department: dept?.name,
          reason: note.trim() || undefined,
        }),
      })
      setConfirmedAppt(appt)
      setBookingState('booked')
      loadAppointments()
    } catch (err) {
      setBookError((err instanceof Error ? err.message : '') || 'Could not send your request. Please try again.')
      setBookingState('idle')
    }
  }

  const handleCancel = async (id: string) => {
    setBookError('')
    try {
      await apiFetch(`/appointments/${id}`, { method: 'DELETE' })
      setUpcoming(prev => prev.filter(a => a.id !== id))
    } catch (err) {
      setBookError((err instanceof Error ? err.message : '') || 'Could not cancel. Please try again.')
    } finally {
      loadAppointments() // reconcile with server truth whether or not the cancel succeeded
    }
  }

  const handleDownloadFhir = async () => {
    const pid = reports?.patient_id
    if (!pid || fhirLoading) return
    setFhirError('')
    setFhirLoading(true)
    try {
      const bundle = await apiFetch<unknown>(`/fhir/patient/${pid}/$everything`)
      const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `orb-record-${pid}.json`
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (err) {
      setFhirError((err instanceof Error ? err.message : '') || 'Could not download your record right now.')
    } finally {
      setFhirLoading(false)
    }
  }

  const handleReset = () => {
    setBookingState('idle')
    setSelectedDate(null)
    setSelectedSlot(null)
    setSelectedDept(null)
    setNote('')
    setBookError('')
    setConfirmedAppt(null)
  }

  const canBook = selectedDate && selectedSlot && selectedDept

  // Times already over on the chosen day are shown but cannot be picked: the
  // server refuses them. Read at render, which the 30 s status poll repeats.
  const now = new Date()
  const openSlots = selectedDate ? TIME_SLOTS.filter(slot => !slotHasPassed(selectedDate, slot, now)) : TIME_SLOTS

  const loadStatus = async () => {
    try {
      const data = await getBridgeStatus()
      setStatus(data)
    } catch (err) {
      console.error('Failed to load status:', err)
    } finally {
      setLoading(false)
    }
  }

  const loadReports = async () => {
    setReportsLoading(true)
    try {
      const data = await getBridgeReports()
      setReports(data)
    } catch (err) {
      console.error('Failed to load reports:', err)
    } finally {
      setReportsLoading(false)
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    loadStatus()
    loadReports()
    loadAppointments()
    const interval = setInterval(loadStatus, 30000)
    return () => clearInterval(interval)
  }, [loadAppointments])

  const handleAsk = async () => {
    if (!question.trim() || asking) return
    setAsking(true)
    setAnswer('')
    try {
      await askBridge(question.trim(), (partial) => {
        setAnswer(partial)
      })
    } catch {
      setAnswer('Sorry, I couldn\'t connect to the assistant. Please ask your nurse or doctor directly.')
    } finally {
      setAsking(false)
    }
  }

  // The level in words, never in colour (ruled 2026-10-03, with the vitals
  // card): the badge and icon were painted green / amber / red, so a patient
  // read their own "critical" in alarm red with nobody beside them. And no
  // stand-in: this used to default to 'stable', which showed a "Stable" badge
  // while loading or after a failed request, a level no score had produced.
  const statusLevel: string | null = status?.status || null

  // Lab results and imaging in one list, newest first. Lab results arrive under
  // `labs`; this list used to read imaging alone, so a patient with charted
  // results was told there were none. A lab result opens like any document.
  const labHistory = useMemo(() => {
    const labs: PortalReport[] = reports?.labs ?? []
    const imaging: PortalReport[] = reports?.imaging_reports ?? []
    const items = [
      ...labs.map(data => ({ type: 'doc' as const, data })),
      ...imaging.map(data => ({ type: 'lab' as const, data })),
    ]
    const at = (item: { data: PortalReport }) => (item.data.created_at ? parseUTC(item.data.created_at).getTime() : 0)
    return items.sort((a, b) => at(b) - at(a))
  }, [reports])

  // A local, so the discharge row's click handler keeps the null check.
  const dischargeSummary = reports?.discharge_summary

  return (
    <div className="bridge">

      <header className="bridge__header glass">
        <div className="bridge__brand">
          <OrbLogo size={28} />
          <span className="bridge__brand-name">Orb Bridge</span>
        </div>
        <div className="bridge__header-right">
          {/* ONE identity on this page. The chrome used to print the ACCOUNT
              name while the body printed the linked PATIENT RECORD's name, and
              on a patient-facing portal two names on one screen reads as
              "I am looking at someone else's chart" — the single worst sentence
              a patient portal can provoke. The clinical record is the
              authority: it is what every vital, medication and care-team line
              below is drawn from. The account name is the fallback only when
              no record has loaded. */}
          <span className="bridge__greeting">
            {status?.patient_name || user?.name}
          </span>
          <button className="bridge__logout" onClick={logout} id="bridge-logout">
            Sign out
          </button>
        </div>
      </header>

      <main className="bridge__content">
        <div className="bridge__welcome animate-slide-up">
          <h1 className="bridge__title">
            Welcome, {status?.patient_name?.split(' ')[0] || user?.name?.split(' ')[0]}
          </h1>
          <p className="bridge__subtitle">
            Here's your care overview. Everything in simple, clear language.
          </p>
        </div>

        <div className="bridge__cards animate-slide-up stagger-2">
          <div className="bridge__card glass-card">
            <div className="bridge__card-icon">
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M3 12h4l3-9 4 18 3-9h4"/>
              </svg>
            </div>
            <h2 className="bridge__card-title">Your Status</h2>
            <p className="bridge__card-text">
              {loading ? 'Loading your status…' : (status?.status_message || 'Your care team is monitoring you.')}
            </p>
            {statusLevel && (
              <span className="bridge__card-badge">
                {statusLevel.charAt(0).toUpperCase() + statusLevel.slice(1)}
              </span>
            )}
          </div>

          <CareTeamCard />

          <VitalsTrendsCard />

          {/* ─── Schedule Follow-Up Appointment Card ─── */}
          <div className="bridge__card bridge__card--appt glass-card" id="bridge-appointment-card">
            <div className="bridge__card-icon bridge__card-icon--appt">
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <rect x="3" y="4" width="18" height="18" rx="2"/>
                <line x1="16" y1="2" x2="16" y2="6"/>
                <line x1="8" y1="2" x2="8" y2="6"/>
                <line x1="3" y1="10" x2="21" y2="10"/>
                <path d="M8 14h.01M12 14h.01M16 14h.01M8 18h.01M12 18h.01"/>
              </svg>
            </div>
            <h2 className="bridge__card-title">Schedule Follow-Up</h2>

            {bookingState === 'booked' ? (
              <div className="appt__confirmed">
                <div className="appt__confirmed-check">
                  <svg width="28" height="28" viewBox="0 0 36 36" fill="none">
                    <circle cx="18" cy="18" r="17" stroke="var(--status-ok)" strokeWidth="2" className="appt__check-circle"/>
                    <path d="M11 18l5 5 9-9" stroke="var(--status-ok)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" className="appt__check-path"/>
                  </svg>
                </div>
                {/* Describes the row that exists: the patient's request, which
                    nobody has agreed yet, or (when the slot was already in the
                    diary) the care team's own booking. Never "Confirmed". */}
                <p className="appt__confirmed-msg">
                  {confirmedAppt?.requested_by_patient === false ? 'Already booked' : 'Request sent'}
                </p>
                <p className="appt__confirmed-details">
                  {confirmedAppt
                    ? `${fmtApptDate(confirmedAppt.requested_date)} · ${fmt12h(confirmedAppt.requested_time)}`
                    : selectedDate && `${MONTHS[selectedDate.getMonth()]} ${selectedDate.getDate()} · ${selectedSlot}`}
                  {confirmedAppt?.department && <><br/>{confirmedAppt.department}</>}
                </p>
                <p className="appt__confirmed-details">
                  {confirmedAppt?.requested_by_patient === false
                    ? 'Your care team already has an appointment for you at this time, so nothing new was added.'
                    : 'Your request is on the care team\'s appointment list. Nobody has agreed this time with you yet, and the team may move or cancel it. This card always shows where it stands.'}
                </p>
                <button className="appt__new-btn" onClick={handleReset} id="appt-new-btn">
                  New
                </button>
              </div>
            ) : (
              <>
                {/* Your upcoming (real) appointments */}
                {upcoming.length > 0 && (
                  <div className="appt__upcoming">
                    <label className="appt__label">Your upcoming appointments</label>
                    <ul className="appt__upcoming-list">
                      {upcoming.map(a => (
                        <li key={a.id} className="appt__upcoming-item">
                          <div className="appt__upcoming-info">
                            <span className="appt__upcoming-when">{fmtApptDate(a.scheduled_date)} · {fmt12h(a.scheduled_time)}</span>
                            {/* A request the patient made is said to be one; its
                                stored type is staff wording. */}
                            {a.origin === 'patient' ? (
                              <>
                                <span className="appt__upcoming-type">{a.department || 'Follow-up'}</span>
                                <span className="appt__upcoming-type">Requested by you. The time is not agreed yet.</span>
                              </>
                            ) : (
                              <>
                                <span className="appt__upcoming-type">{a.department || a.appointment_type_label || a.appointment_type}</span>
                                {/* The reason the care team wrote, as they wrote it. */}
                                {a.reason && <span className="appt__upcoming-reason">{a.reason}</span>}
                              </>
                            )}
                          </div>
                          <button
                            className="appt__upcoming-cancel"
                            onClick={() => handleCancel(a.id)}
                            aria-label={`Cancel appointment on ${fmtApptDate(a.scheduled_date)}`}
                          >
                            Cancel
                          </button>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                {/* Week Strip Calendar */}
                <div className="appt__calendar">
                  <div className="appt__cal-header">
                    <button className="appt__cal-nav" onClick={() => handleWeekNav(-1)} id="appt-prev-week" aria-label="Previous week">
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="15 18 9 12 15 6"/></svg>
                    </button>
                    <span className="appt__cal-month">{weekMonthLabel(weekDates)}</span>
                    <button className="appt__cal-nav" onClick={() => handleWeekNav(1)} id="appt-next-week" aria-label="Next week">
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
                    </button>
                  </div>
                  <div className="appt__week-strip">
                    {weekDates.map((date, i) => (
                      <button
                        key={i}
                        className={`appt__week-cell${selectedDate && isSameDay(date, selectedDate) ? ' appt__week-cell--selected' : ''}${isSameDay(date, today) ? ' appt__week-cell--today' : ''}${isDateDisabled(date) ? ' appt__week-cell--disabled' : ''}`}
                        onClick={() => {
                          if (isDateDisabled(date)) return
                          setSelectedDate(date)
                          // A time kept from another day may already be over on this one.
                          if (selectedSlot && slotHasPassed(date, selectedSlot, new Date())) setSelectedSlot(null)
                        }}
                        disabled={isDateDisabled(date)}
                      >
                        <span className="appt__week-day">{DAYS[date.getDay()]}</span>
                        <span className="appt__week-num">{date.getDate()}</span>
                      </button>
                    ))}
                  </div>
                </div>

                {!selectedDate && (
                  <p className="appt__hint">Choose a day for your follow-up.</p>
                )}

                {/* Time Slots */}
                {selectedDate && (
                  <div className="appt__section appt__section--animate">
                    <label className="appt__label">Pick a time</label>
                    <div className="appt__slots">
                      {TIME_SLOTS.map(slot => (
                        <button
                          key={slot}
                          className={`appt__slot${selectedSlot === slot ? ' appt__slot--active' : ''}`}
                          onClick={() => setSelectedSlot(slot)}
                          disabled={!openSlots.includes(slot)}
                        >
                          {slot}
                        </button>
                      ))}
                    </div>
                    {openSlots.length === 0 && (
                      <p className="appt__hint">No times are left today. Choose another day.</p>
                    )}
                  </div>
                )}

                {/* Department */}
                {selectedSlot && (
                  <div className="appt__section appt__section--animate">
                    <label className="appt__label">Department</label>
                    <div className="appt__depts">
                      {DEPARTMENTS.map(dept => (
                        <button
                          key={dept.id}
                          className={`appt__dept${selectedDept === dept.id ? ' appt__dept--active' : ''}`}
                          onClick={() => setSelectedDept(dept.id)}
                        >
                          <span className="appt__dept-icon">{dept.icon}</span>
                          <span className="appt__dept-name">{dept.label}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                )}

                {/* Optional note */}
                {selectedDept && (
                  <div className="appt__section appt__section--animate">
                    <label className="appt__label">Note for your doctor <span className="appt__optional">(optional)</span></label>
                    <textarea
                      className="appt__note"
                      placeholder="e.g. I'd like to discuss my medication…"
                      value={note}
                      onChange={e => setNote(e.target.value)}
                      rows={2}
                      maxLength={1000}
                      id="appt-note"
                    />
                  </div>
                )}

                {/* Book button */}
                {canBook && (
                  <button
                    className={`appt__book-btn${bookingState === 'confirming' ? ' appt__book-btn--loading' : ''}`}
                    onClick={handleBook}
                    disabled={bookingState === 'confirming'}
                    id="appt-book-btn"
                  >
                    {bookingState === 'confirming' ? (
                      <span className="appt__spinner" />
                    ) : (
                      <>
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M5 13l4 4L19 7"/>
                        </svg>
                        Send Request
                      </>
                    )}
                  </button>
                )}

                {bookError && (
                  <p className="appt__error" role="alert">{bookError}</p>
                )}
              </>
            )}
          </div>
        </div>

        {/* ─── My Records Panel ─── */}
        <div className="bridge__reports animate-slide-up stagger-2">
          <div className="bridge__card glass-card" style={{ padding: '20px 22px' }}>
            <h2 className="bridge__reports-title">My Records</h2>
            <div className="detail__actions" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', marginTop: '16px' }}>
              <button className="detail__action-btn" onClick={() => setMedsModalOpen(true)} id="bridge-btn-meds">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M10.5 1.5H8.25A2.25 2.25 0 006 3.75v16.5a2.25 2.25 0 002.25 2.25h7.5A2.25 2.25 0 0018 20.25V3.75a2.25 2.25 0 00-2.25-2.25H13.5m-3 0V3h3V1.5m-3 0h3m-6 9h6m-6 4h3"/></svg>
                Medications
              </button>
              <button className="detail__action-btn" onClick={() => setDocsModalOpen(true)} id="bridge-btn-docs">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
                Report History
              </button>
              <button className="detail__action-btn" onClick={() => setLabsModalOpen(true)} id="bridge-btn-labs">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 3h6v7l4 9H5l4-9V3z"/><line x1="9" y1="3" x2="15" y2="3"/></svg>
                Lab History
              </button>
              <button className="detail__action-btn" onClick={() => setDischargeModalOpen(true)} id="bridge-btn-discharge">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
                Discharge Summary
              </button>
            </div>
            <button
              className="bridge__fhir-btn"
              onClick={handleDownloadFhir}
              disabled={fhirLoading || !reports?.patient_id}
              id="bridge-btn-fhir"
              style={{ marginTop: 12 }}
            >
              {fhirLoading ? (
                <span className="appt__spinner" />
              ) : (
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              )}
              Download my health record
            </button>
            {fhirError && (
              <p className="appt__error" role="alert" style={{ marginTop: 8 }}>{fhirError}</p>
            )}
          </div>
        </div>




        {answer && (
          <div className="bridge__answer animate-slide-up glass-card">
            <div className="bridge__answer-header">
              <OrbLogo size={20} />
              <span>Orb Bridge</span>
            </div>
            <pre className="bridge__answer-text">{answer}</pre>
          </div>
        )}

        <div className="bridge__ask animate-slide-up stagger-3">
          <div className="bridge__ask-input glass">
            <input
              type="text"
              placeholder="What would you like to know about your care?"
              className="bridge__input"
              value={question}
              onChange={e => setQuestion(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleAsk()}
              disabled={asking}
              id="bridge-ask-input"
            />
            <button className="bridge__ask-btn" onClick={handleAsk} disabled={asking} id="bridge-ask-btn">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <line x1="22" y1="2" x2="11" y2="13"/>
                <polygon points="22 2 15 22 11 13 2 9 22 2"/>
              </svg>
            </button>
          </div>
        </div>
      </main>

      {/* ─── Modals (outside bridge__content to avoid stacking context) ─── */}
      {medsModalOpen && (
        <PortalModal
          overlayClass={`modal-overlay${closingModal === 'meds' ? ' modal-overlay--closing' : ''}`}
          modalClass={`modal glass-card${closingModal === 'meds' ? ' modal--closing' : ''}`}
          style={{ maxWidth: 840, width: '100%' }}
          labelledBy="portal-meds-title"
          onClose={() => closeModal(setMedsModalOpen, 'meds')}
        >
            <h2 className="modal__title" id="portal-meds-title">Medication History</h2>
            <div className="modal__body" style={{ maxHeight: '70vh', display: 'flex', flexDirection: 'column' }}>
              <div className="detail__reports-list" style={{ overflowY: 'auto' }}>
                {reportsLoading ? (
                  <div className="bridge__report-panel">{[1,2,3].map(i => <div key={i} className="bridge__report-skeleton" style={{ marginBottom: 6 }} />)}</div>
                ) : (reports?.medications?.length || 0) === 0 ? (
                  <div className="detail__no-data">No medications on record</div>
                ) : (
                  reports?.medications.map((m, i) => (
                    /* Real <button>s. These rows carry cursor:pointer and a hover
                       fill but were <div onClick>, so on the patient portal every
                       medication, document, lab and discharge row was mouse-only:
                       Tab skipped the entire list and there is no other way to
                       open the detail. */
                    <button
                      type="button"
                      key={`med-${i}`}
                      className="detail__report bridge__report-item"
                      onClick={() => setSelectedReportDetail({ type: 'med', data: m })}
                    >
                      <span className="bridge__report-item-icon bridge__report-item-icon--med" aria-hidden><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M10.5 1.5H8.25A2.25 2.25 0 006 3.75v16.5a2.25 2.25 0 002.25 2.25h7.5A2.25 2.25 0 0018 20.25V3.75a2.25 2.25 0 00-2.25-2.25H13.5"/></svg></span>
                      <span className="detail__report-info bridge__report-item-body">
                        <span className="detail__report-title bridge__report-item-name">{m.drug_name}</span>
                        <span className="detail__note-date bridge__report-item-meta">{m.dosage} · {m.route} · {m.frequency}</span>
                      </span>
                      <span className={`bridge__report-item-badge bridge__report-item-badge--${m.status}`}>{m.status}</span>
                    </button>
                  ))
                )}
              </div>
            </div>
            <div className="modal__actions">
              <button className="modal__btn bridge__modal-close-btn" onClick={() => closeModal(setMedsModalOpen, 'meds')}>Close</button>
            </div>
        </PortalModal>
      )}

      {docsModalOpen && (
        <PortalModal
          overlayClass={`modal-overlay${closingModal === 'docs' ? ' modal-overlay--closing' : ''}`}
          modalClass={`modal glass-card${closingModal === 'docs' ? ' modal--closing' : ''}`}
          style={{ maxWidth: 840, width: '100%' }}
          labelledBy="portal-docs-title"
          onClose={() => closeModal(setDocsModalOpen, 'docs')}
        >
            <h2 className="modal__title" id="portal-docs-title">Clinical Report History</h2>
            <div className="modal__body" style={{ maxHeight: '70vh', display: 'flex', flexDirection: 'column' }}>
              <div className="detail__reports-list" style={{ overflowY: 'auto' }}>
                {reportsLoading ? (
                  <div className="bridge__report-panel">{[1,2,3].map(i => <div key={i} className="bridge__report-skeleton" style={{ marginBottom: 6 }} />)}</div>
                ) : (reports?.clinical_documents?.length || 0) === 0 ? (
                  <div className="detail__no-data">No documents on record</div>
                ) : (
                  reports?.clinical_documents.map((d, i) => (
                    <button
                      type="button"
                      key={`doc-${i}`}
                      className="detail__report bridge__report-item"
                      onClick={() => setSelectedReportDetail({ type: 'doc', data: d })}
                    >
                      <span className="bridge__report-item-icon bridge__report-item-icon--doc" aria-hidden><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/></svg></span>
                      <span className="detail__report-info bridge__report-item-body">
                        <span className="detail__report-title bridge__report-item-name">{d.title}</span>
                        <span className="detail__note-date bridge__report-item-meta">{d.type_label || 'Clinical note'} · {formatClinicalDate(d.created_at)}</span>
                      </span>
                    </button>
                  ))
                )}
              </div>
            </div>
            <div className="modal__actions">
              <button className="modal__btn bridge__modal-close-btn" onClick={() => closeModal(setDocsModalOpen, 'docs')}>Close</button>
            </div>
        </PortalModal>
      )}

      {labsModalOpen && (
        <PortalModal
          overlayClass={`modal-overlay${closingModal === 'labs' ? ' modal-overlay--closing' : ''}`}
          modalClass={`modal glass-card${closingModal === 'labs' ? ' modal--closing' : ''}`}
          style={{ maxWidth: 840, width: '100%' }}
          labelledBy="portal-labs-title"
          onClose={() => closeModal(setLabsModalOpen, 'labs')}
        >
            <h2 className="modal__title" id="portal-labs-title">Lab &amp; Imaging History</h2>
            <div className="modal__body" style={{ maxHeight: '70vh', display: 'flex', flexDirection: 'column' }}>
              <div className="detail__reports-list" style={{ overflowY: 'auto' }}>
                {reportsLoading ? (
                  <div className="bridge__report-panel">{[1,2,3].map(i => <div key={i} className="bridge__report-skeleton" style={{ marginBottom: 6 }} />)}</div>
                ) : labHistory.length === 0 ? (
                  <div className="detail__no-data">No lab or imaging reports yet</div>
                ) : (
                  labHistory.map((item, i) => (
                    <button
                      type="button"
                      key={`lab-${i}`}
                      className="detail__report bridge__report-item"
                      onClick={() => setSelectedReportDetail(item)}
                    >
                      <span className="bridge__report-item-icon bridge__report-item-icon--lab" aria-hidden><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M9 3h6v7l4 9H5l4-9V3z"/></svg></span>
                      {item.type === 'doc' ? (
                        <span className="detail__report-info bridge__report-item-body">
                          <span className="detail__report-title bridge__report-item-name">{item.data.title}</span>
                          <span className="detail__note-date bridge__report-item-meta">{item.data.type_label || 'Lab result'} · {formatClinicalDate(item.data.created_at)}{item.data.source === 'simulated' ? ' · Simulated result, not from your care' : ''}</span>
                        </span>
                      ) : (
                        <span className="detail__report-info bridge__report-item-body">
                          <span className="detail__report-title bridge__report-item-name">{item.data.image_type?.toUpperCase() || 'Report'}: {item.data.clinical_context || 'General'}</span>
                          <span className="detail__note-date bridge__report-item-meta">{formatClinicalDate(item.data.created_at)}</span>
                        </span>
                      )}
                    </button>
                  ))
                )}
              </div>
            </div>
            <div className="modal__actions">
              <button className="modal__btn bridge__modal-close-btn" onClick={() => closeModal(setLabsModalOpen, 'labs')}>Close</button>
            </div>
        </PortalModal>
      )}

      {dischargeModalOpen && (
        <PortalModal
          overlayClass={`modal-overlay${closingModal === 'discharge' ? ' modal-overlay--closing' : ''}`}
          modalClass={`modal glass-card${closingModal === 'discharge' ? ' modal--closing' : ''}`}
          style={{ maxWidth: 840, width: '100%' }}
          labelledBy="portal-discharge-title"
          onClose={() => closeModal(setDischargeModalOpen, 'discharge')}
        >
            <h2 className="modal__title" id="portal-discharge-title">Discharge Summary</h2>
            <div className="modal__body" style={{ maxHeight: '70vh', display: 'flex', flexDirection: 'column' }}>
              <div className="detail__reports-list" style={{ overflowY: 'auto' }}>
                {reportsLoading ? (
                  <div className="bridge__report-panel">{[1].map(i => <div key={i} className="bridge__report-skeleton" style={{ marginBottom: 6 }} />)}</div>
                ) : !dischargeSummary ? (
                  <div className="detail__no-data">No discharge summary available yet.</div>
                ) : (
                  <button
                    type="button"
                    className="detail__report bridge__report-item"
                    onClick={() => setSelectedReportDetail({ type: 'discharge', data: dischargeSummary })}
                  >
                    <span className="bridge__report-item-icon bridge__report-item-icon--discharge" aria-hidden><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M5 13l4 4L19 7"/></svg></span>
                    <span className="detail__report-info bridge__report-item-body">
                      <span className="detail__report-title bridge__report-item-name">Discharge Summary</span>
                      <span className="detail__note-date bridge__report-item-meta">{dischargeSummary.discharged_at ? `Discharged ${formatClinicalDate(dischargeSummary.discharged_at)}` : 'Available'}</span>
                    </span>
                    {/* Ready only once a clinician has reviewed the drafted
                        text; until then the server withholds it. */}
                    {dischargeSummary.status === 'reviewed'
                      ? <span className="bridge__report-item-badge bridge__report-item-badge--active">Ready</span>
                      : <span className="bridge__report-item-badge" style={{ background: 'var(--bg-elevated)', color: 'var(--text-secondary)', border: '1px solid var(--border-subtle)' }}>Being finalised</span>}
                  </button>
                )}
              </div>
            </div>
            <div className="modal__actions">
              <button className="modal__btn bridge__modal-close-btn" onClick={() => closeModal(setDischargeModalOpen, 'discharge')}>Close</button>
            </div>
        </PortalModal>
      )}

      {/* Detail Modal */}
      {selectedReportDetail && (
        <PortalModal
          overlayClass={`bridge__report-modal-overlay${closingModal === 'detail' ? ' bridge__report-modal-overlay--closing' : ''}`}
          modalClass={`bridge__report-modal${closingModal === 'detail' ? ' bridge__report-modal--closing' : ''}`}
          labelledBy="portal-detail-title"
          onClose={closeDetail}
        >
            <div className="bridge__report-modal-header">
              <h3 className="bridge__report-modal-title" id="portal-detail-title">
                {selectedReportDetail.type === 'med' && selectedReportDetail.data.drug_name}
                {selectedReportDetail.type === 'doc' && selectedReportDetail.data.title}
                {selectedReportDetail.type === 'lab' && `${selectedReportDetail.data.image_type?.toUpperCase() || 'Report'}`}
                {selectedReportDetail.type === 'discharge' && 'Discharge Summary'}
              </h3>
              <button type="button" className="bridge__report-modal-close" onClick={closeDetail} aria-label="Close">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
              </button>
            </div>
            <div className="bridge__report-modal-content">
              {selectedReportDetail.type === 'med' && (<>{`Drug: ${selectedReportDetail.data.drug_name}\nDosage: ${selectedReportDetail.data.dosage}\nRoute: ${selectedReportDetail.data.route}\nFrequency: ${selectedReportDetail.data.frequency}\nStatus: ${selectedReportDetail.data.status}`}{selectedReportDetail.data.started_at && `\nStarted: ${formatClinicalDate(selectedReportDetail.data.started_at)}`}</>)}
              {selectedReportDetail.type === 'doc' && selectedReportDetail.data.content}
              {selectedReportDetail.type === 'lab' && (selectedReportDetail.data.analysis || 'No analysis available.')}
              {selectedReportDetail.type === 'discharge' && (
                selectedReportDetail.data.summary
                || selectedReportDetail.data.message
                || 'Your discharge summary is being finalised by your doctor.'
              )}
            </div>
            <div className="bridge__report-modal-meta">
              {selectedReportDetail.type === 'doc' && `Type: ${selectedReportDetail.data.type_label || 'Clinical note'} · ${formatClinicalDateTime(selectedReportDetail.data.created_at)}${selectedReportDetail.data.source === 'simulated' ? ' · Simulated result, not from your care' : ''}`}
              {selectedReportDetail.type === 'lab' && `${selectedReportDetail.data.clinical_context || ''} · ${formatClinicalDateTime(selectedReportDetail.data.created_at)}`}
              {selectedReportDetail.type === 'discharge' && selectedReportDetail.data.discharged_at && `Discharged: ${formatClinicalDateTime(selectedReportDetail.data.discharged_at)}`}
            </div>
        </PortalModal>
      )}
    </div>
  )
}
