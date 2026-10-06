/**
 * Orb Surgical Suite — 3-Tier Drill-Down Panel (Dense UI-UX Pro Max)
 */
import { useState, useEffect, useCallback, useRef } from 'react'
import {
  getSurgerySchedule, getSurgeryDetail, getSurgeryRiskAssessment, prepForSurgery, getEmergencyQueue, getSurgeryCases, alertSurgicalTeam, startSurgery, completeSurgery, socket,
  type EmergencySurgeryCase, type SurgeryCase, type SurgeryImportedContext,
} from '../../api/client'
import { useVisiblePolling } from '../../hooks/useVisiblePolling'
import { formatClinicalClock, formatClinicalDayMonth } from '../../lib/datetime'
import Icon from '../../components/common/Icon'
import './SurgicalSuitePanel.css'
import EmergencyIntakePopup from '../../components/surgery/EmergencyIntakePopup'
import WhoSurgicalChecklist from '../../components/surgery/WhoSurgicalChecklist'

/* ━━━ Success probability — one unit, decided here ━━━━━━━━━━━━━━━━━━━━━━━━━
 *
 * `surgeries.success_probability` is stored as a FRACTION (the seeded
 * Exploratory Laparotomy is 0.88) and every reader here treated it as a
 * percentage. The list row rendered "0.88% success" for an operation with an
 * 88% predicted success, the ring drew a 0.88/100 arc, and the three colour
 * thresholds (>=95 ok, >=85 warn, else danger) put every case in DANGER RED
 * for the same reason. A surgeon reading their morning list saw every
 * procedure marked as near-certain to fail.
 *
 * Normalising at the read is deliberate: the column has no unit declared and
 * older rows may already be 0–100, so a value at or below 1 is read as a
 * fraction and anything above it is left alone. A probability of exactly 1.0
 * resolves to 100%, which is the right answer under either reading.
 */
function successPct(raw: number | null | undefined): number | null {
  if (raw === null || raw === undefined || Number.isNaN(raw)) return null
  return raw <= 1 ? Math.round(raw * 100) : Math.round(raw)
}

function successColor(pct: number | null): string {
  if (pct === null) return 'var(--text-muted)'
  if (pct >= 95) return 'var(--status-ok-text)'
  if (pct >= 85) return 'var(--status-warn-text)'
  return 'var(--status-danger-text)'
}

/* ━━━ Types ━━━ */
interface SurgeryBrief {
  id: string
  patient_id: string
  patient_name: string
  age: number
  sex: string
  mrn: string
  procedure_name: string
  diagnosis: string
  scheduled_time: string
  estimated_duration_min: number
  operating_room: string
  status: string
  /** The NEWS2 band of the patient's latest vitals (the Vigil board's rule),
   *  or null when nothing is recorded: "not scored", never "stable". */
  risk_level: string | null
  risk_score: number | null
  /** The NEWS2 behind that band, and whether the set was complete. */
  news2?: number | null
  news2_complete?: boolean | null
  /** The operating surgeon's name, as the users table holds it. */
  surgeon_name?: string | null
  success_probability: number | null
}

interface DaySchedule {
  date: string
  day_label: string
  /** Decided by the server in the HOSPITAL's timezone. Do not recompute it from
   *  `date` here: this browser's calendar day is not necessarily the ward's. */
  is_today: boolean
  surgeries: SurgeryBrief[]
}

interface SurgeryFull {
  id: string
  patient_id: string
  patient_name: string
  age: number
  sex: string
  mrn: string
  bed: string | null
  ward: string | null
  patient_diagnosis: string
  admission_date: string | null
  procedure_name: string
  diagnosis: string
  scheduled_date: string
  scheduled_time: string
  estimated_duration_min: number
  operating_room: string
  status: string
  co_morbidities: string[]
  allergies: string[]
  surgical_checklist: string[]
  instruments_required: string[]
  staff_required: { role: string; name: string }[]
  risk_assessment: string | null
  possible_complications: string[]
  success_probability: number | null
  risk_level: string
  risk_score: number | null
  risk_drivers: string[]
  notes: string | null
  imported_context?: SurgeryImportedContext
}

/* ━━━ Helpers ━━━ */
// The clinical "08:00 AM", the same form every other ops screen uses.
function formatTime(t: string) {
  return formatClinicalClock(t)
}

function formatDuration(mins: number) {
  const h = Math.floor(mins / 60)
  const m = mins % 60
  return h > 0 ? `${h}h ${m > 0 ? m + 'm' : ''}` : `${m}m`
}

function riskColor(level: string | null | undefined) {
  if (level == null) return 'var(--text-muted)'
  if (level === 'critical') return 'var(--status-danger)'
  if (level === 'elevated') return 'var(--status-warn)'
  return 'var(--status-ok)'
}

/** "critical · NEWS2 9" from the latest vitals; "≥" for an incomplete set;
 *  "not scored" when nothing is recorded. */
function scheduleBand(s: Pick<SurgeryBrief, 'risk_level' | 'news2' | 'news2_complete'>): string {
  if (!s.risk_level) return 'not scored'
  if (typeof s.news2 !== 'number') return s.risk_level
  return `${s.risk_level} · NEWS2 ${s.news2_complete === false ? '≥' : ''}${s.news2}`
}

function formatDate(dateStr: string) {
  const d = new Date(dateStr + 'T00:00:00')
  return formatClinicalDayMonth(d)
}

/* ━━━ Component ━━━ */
export default function SurgicalSuitePanel() {
  const [schedule, setSchedule] = useState<DaySchedule[]>([])
  const [emergencyQueue, setEmergencyQueue] = useState<EmergencySurgeryCase[]>([])
  const [intakePopupData, setIntakePopupData] = useState<{ surgeryId: string } | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // Active Cases: the escalation flow
  const [activeView, setActiveView] = useState<'schedule' | 'cases'>('schedule')
  // Keep a ref in sync so the mount-only fallback poll below reads the current view
  // instead of the value captured at first render.
  const activeViewRef = useRef(activeView)
  useEffect(() => { activeViewRef.current = activeView }, [activeView])
  const [activeCases, setActiveCases] = useState<SurgeryCase[]>([])
  const [casesLoading, setCasesLoading] = useState(false)
  // Why the last team alert, start or completion failed, shown on that case
  // card. A browser alert() blocked the screen and the reason was then lost.
  const [caseError, setCaseError] = useState<{ id: string; message: string } | null>(null)

  // Tier state
  const [tier, setTier] = useState<1 | 2 | 3>(1)
  const [selectedDayIdx, setSelectedDayIdx] = useState<number | null>(null)
  const [selectedSurgery, setSelectedSurgery] = useState<SurgeryFull | null>(null)
  const [surgeryLoading, setSurgeryLoading] = useState(false)

  // Risk assessment & Prep
  const [riskText, setRiskText] = useState('')
  const [riskLoading, setRiskLoading] = useState(false)
  const [riskExpanded, setRiskExpanded] = useState(false)
  
  const [prepLoading, setPrepLoading] = useState(false)
  const [prepSuccess, setPrepSuccess] = useState(false)

  // Animation
  const [animState, setAnimState] = useState<'idle' | 'expanding' | 'collapsing' | 'detail-in' | 'detail-out'>('idle')
  const panelRef = useRef<HTMLDivElement>(null)

  // Parse ai_package_status from surgery notes JSON
  const getAiPackageStatus = (surgery: SurgeryFull | null): string | null => {
    if (!surgery?.notes) return null
    try {
      const parsed = JSON.parse(surgery.notes)
      return parsed.ai_package_status || null
    } catch { return null }
  }
  const packageUnavailable = (status: string | null) =>
    status === 'fallback' || status === 'failed' || status === 'timeout'

  // Tier 3 progressive hydration: poll while enrichment is in-progress. The
  // poll stops by itself once the fetched status leaves the generating states,
  // and stands down while another panel is on screen.
  const isGenerating = selectedSurgery?.status === 'emergency_context_generating' || selectedSurgery?.status === 'emergency_pending'
  useVisiblePolling(async () => {
    if (!selectedSurgery) return
    try {
      setSelectedSurgery(await getSurgeryDetail(selectedSurgery.id))
    } catch (e) {
      console.error('Tier 3 hydration poll error', e)
    }
  }, 3000, !!selectedSurgery && tier === 3 && isGenerating)

  const loadCases = async () => {
    setCasesLoading(true)
    try {
      const cases = await getSurgeryCases()
      setActiveCases(cases)
    } catch (e) {
      console.error('Failed to load surgery cases', e)
    } finally {
      setCasesLoading(false)
    }
  }

  const loadEmergencyQueue = async () => {
    try {
      const queue = await getEmergencyQueue()
      setEmergencyQueue(queue)
      // If popup is open, we can hydrate its details from the queue
    } catch (e) {
      console.error('Failed to load emergency queue', e)
    }
  }

  const loadSchedule = async () => {
    setLoading(true)
    try {
      const data = await getSurgerySchedule(14)
      setSchedule(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    loadSchedule()
    loadEmergencyQueue()
    loadCases()
    
    // Socket-first: immediate re-fetch on emergency intake event
    const handleEmergencyIntake = (data: { surgery_id: string }) => {
      loadEmergencyQueue()
      loadCases()
      setIntakePopupData({ surgeryId: data.surgery_id })
    }
    
    socket.on('surgery:emergency_intake', handleEmergencyIntake)

    return () => {
      socket.off('surgery:emergency_intake', handleEmergencyIntake)
    }
  }, [])

  // Fallback poll: catch missed socket events (e.g. reconnect, tab switch).
  useVisiblePolling(() => {
    loadEmergencyQueue()
    if (activeViewRef.current === 'cases') loadCases()
  }, 10000)

  /* ── Day click → Tier 2 ── */
  const handleDayClick = useCallback((idx: number) => {
    setSelectedDayIdx(idx)
    setAnimState('expanding')
    setTimeout(() => {
      setTier(2)
      setAnimState('idle')
    }, 450)
  }, [])

  /* ── Back to Tier 1 ── */
  const handleBackToWeek = useCallback(() => {
    setAnimState('collapsing')
    setTimeout(() => {
      setTier(1)
      setSelectedDayIdx(null)
      setAnimState('idle')
    }, 400)
  }, [])

  const abortControllerRef = useRef<AbortController | null>(null)

  /* ── Load risk assessment ── */
  const loadRiskAssessment = useCallback(async (surgeryId: string) => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort()
    }
    abortControllerRef.current = new AbortController()
    const signal = abortControllerRef.current.signal

    setRiskLoading(true)
    setRiskText('')
    try {
      await getSurgeryRiskAssessment(surgeryId, (chunk) => {
        if (!signal.aborted) {
          setRiskText(chunk)
        }
      }, signal)
    } catch (e) {
      const aborted = typeof e === 'object' && e !== null && 'name' in e && e.name === 'AbortError'
      if (!aborted) {
        setRiskText('Assessment unavailable. The AI pre-operative risk assessment could not be generated.')
      }
    } finally {
      if (!signal.aborted) {
        setRiskLoading(false)
      }
    }
  }, [])

  // Tier 3: stream the MedGemma AI risk assessment once per surgery open.
  // Keyed on surgery id + tier so it fires exactly once when a surgery is
  // drilled into, and does not re-fire on unrelated re-renders (streaming
  // chunks, expand toggle, poll updates). loadRiskAssessment is a stable
  // useCallback, so its identity never changes.
  useEffect(() => {
    if (tier !== 3 || !selectedSurgery?.id) return
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    loadRiskAssessment(selectedSurgery.id)
  }, [selectedSurgery?.id, tier, loadRiskAssessment])

  /* ── Surgery click → Tier 3 ── */
  const handleSurgeryClick = useCallback(async (surgeryId: string) => {
    setSurgeryLoading(true)
    setAnimState('detail-in')
    setRiskExpanded(false)
    setPrepSuccess(false)
    try {
      // The case always opens, whatever happened to the prep package. A failed
      // package used to raise a "Feature under development" modal INSTEAD of the
      // case, which hid the patient and the WHO Surgical Safety Checklist from
      // the team about to operate. The risk section says the assessment is
      // unavailable; everything else is still the case.
      const detail = await getSurgeryDetail(surgeryId)
      setSelectedSurgery(detail)
      setTier(3)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      setAnimState('idle')
    } finally {
      setSurgeryLoading(false)
      setTimeout(() => setAnimState('idle'), 500)
    }
  }, [])

  /* ── Back from Tier 3 (to Tier 2 if we came from a day, or Tier 1 if from emergency banner) ── */
  const handleBackToDay = useCallback(() => {
    setAnimState('detail-out')
    setTimeout(() => {
      if (selectedDayIdx !== null) {
        setTier(2)
      } else {
        setTier(1)
      }
      setSelectedSurgery(null)
      setRiskText('')
      setAnimState('idle')
    }, 400)
  }, [selectedDayIdx])

  /* ── Trigger Prep Alert ── */
  const handlePrepForSurgery = useCallback(async () => {
    if (!selectedSurgery) return
    setPrepLoading(true)
    try {
      await prepForSurgery(selectedSurgery.id)
      setPrepSuccess(true)
      setTimeout(() => setPrepSuccess(false), 5000)
    } catch (e) {
      console.error(e)
    } finally {
      setPrepLoading(false)
    }
  }, [selectedSurgery])

  /* ─── Render ─── */
  if (loading) {
    return (
      <div className="suite">
        <div className="suite__loading">
          {[...Array(7)].map((_, i) => (
            <div key={i} className="suite__shimmer-card" style={{ animationDelay: `${i * 0.08}s` }} />
          ))}
        </div>
      </div>
    )
  }

  if (error && !schedule.length) {
    return (
      <div className="suite">
        <div className="suite__error glass-card">
          <span role="alert"><Icon name="alert" size={14} /> {error}</span>
          <button onClick={loadSchedule} className="suite__retry">Retry</button>
        </div>
      </div>
    )
  }

  return (
    <div className="suite" ref={panelRef}>

      {/* ═══ View Tabs ═══ */}
      {tier === 1 && (
        <div style={{ display: 'flex', gap: '4px', marginBottom: '12px', padding: '0 4px' }}>
          <button
            onClick={() => setActiveView('schedule')}
            style={{
              padding: '8px 20px', borderRadius: '8px', cursor: 'pointer',
              fontSize: '0.82rem', fontWeight: 600, fontFamily: 'inherit', transition: 'all 0.2s',
              background: activeView === 'schedule' ? 'var(--accent-deep)' : 'var(--bg-glass)',
              border: activeView === 'schedule' ? 'none' : '1px solid var(--border-glass)',
              color: activeView === 'schedule' ? 'var(--btn-primary-text)' : 'var(--text-secondary)',
            }}
          >
            Schedule
          </button>
          <button
            onClick={() => { setActiveView('cases'); loadCases(); }}
            style={{
              padding: '8px 20px', borderRadius: '8px', cursor: 'pointer',
              fontSize: '0.82rem', fontWeight: 600, fontFamily: 'inherit', transition: 'all 0.2s',
              background: activeView === 'cases' ? 'var(--accent-deep)' : 'var(--bg-glass)',
              border: activeView === 'cases' ? 'none' : '1px solid var(--border-glass)',
              color: activeView === 'cases' ? 'var(--btn-primary-text)' : 'var(--text-secondary)',
              position: 'relative',
            }}
          >
            Active Cases
            {activeCases.length > 0 && (
              <span style={{
                position: 'absolute', top: -4, right: -4,
                width: 18, height: 18, borderRadius: '50%',
                background: 'var(--status-danger)', color: 'var(--status-on-fill)', fontSize: '0.65rem',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                fontWeight: 700,
              }}>
                {activeCases.length}
              </span>
            )}
          </button>
        </div>
      )}

      {/* ═══ ACTIVE CASES VIEW ═══ */}
      {activeView === 'cases' && tier === 1 && (
        <div className="suite__cases animate-fade-in" style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          {casesLoading && activeCases.length === 0 ? (
            <div className="orb-panel-loading">Loading surgical cases…</div>
          ) : activeCases.length === 0 ? (
            <div className="suite__empty glass-card">
              <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="1" strokeLinecap="round" strokeLinejoin="round">
                <rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/>
              </svg>
              <span>No active surgical cases</span>
            </div>
          ) : (
            activeCases.map((c) => {
              const team = c.team || [];
              const allConfirmed = team.length > 0 && team.every((t) => t.status === 'confirmed');
              const hasAlerted = team.some((t) => t.status === 'alerted');
              const isEmergency = (c.status || '').startsWith('emergency');
              const isInProgress = c.status === 'in_progress';

              return (
                <div key={c.id} className="glass-card" style={{
                  padding: '16px 20px',
                  borderColor: isEmergency ? 'color-mix(in srgb, var(--status-danger) 28%, transparent)' : isInProgress ? 'color-mix(in srgb, var(--status-ok) 28%, transparent)' : 'var(--border-glass)',
                  position: 'relative',
                  overflow: 'hidden',
                }}>
                  {/* Status Ribbon */}
                  <div style={{
                    position: 'absolute', top: 0, right: 0,
                    padding: '3px 14px', fontSize: '0.65rem', fontWeight: 700,
                    textTransform: 'uppercase', letterSpacing: '0.06em',
                    background: isInProgress ? 'color-mix(in srgb, var(--status-ok) 10%, transparent)' : isEmergency ? 'color-mix(in srgb, var(--status-danger) 10%, transparent)' : 'var(--bg-glass)',
                    color: isInProgress ? 'var(--status-ok-text)' : isEmergency ? 'var(--status-danger-text)' : 'var(--text-secondary)',
                    borderBottomLeftRadius: 8,
                  }}>
                    {isInProgress ? '● In Progress' : c.status?.replace(/_/g, ' ')}
                  </div>

                  {/* Patient + Procedure Header */}
                  <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '10px' }}>
                    <div style={{
                      width: 38, height: 38, borderRadius: '50%',
                      background: 'linear-gradient(135deg, color-mix(in srgb, var(--status-ok) 20%, transparent), color-mix(in srgb, var(--accent-cyan) 20%, transparent))',
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      fontSize: '0.95rem', fontWeight: 700, color: 'var(--text-primary)', flexShrink: 0,
                    }}>
                      {(c.patient_name || '?')[0]}
                    </div>
                    <div>
                      <div style={{ fontWeight: 600, fontSize: '0.95rem', color: 'var(--text-primary)' }}>
                        {c.patient_name}
                        <span style={{ fontWeight: 400, fontSize: '0.78rem', color: 'var(--text-muted)', marginLeft: 8 }}>
                          {c.age}y/{c.sex} · MRN: {c.mrn}
                        </span>
                      </div>
                      <div style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', marginTop: 2 }}>
                        {c.procedure_name}
                      </div>
                    </div>
                  </div>

                  {/* Diagnosis + Clinical Info */}
                  <div style={{ fontSize: '0.78rem', color: 'var(--text-muted)', marginBottom: '12px', padding: '8px 10px', background: 'var(--bg-glass)', borderRadius: 6, borderLeft: '2px solid var(--accent-gold)' }}>
                    <strong style={{ color: 'var(--text-secondary)' }}>Diagnosis:</strong> {c.diagnosis}
                    {c.operating_room && <span style={{ marginLeft: 12 }}>OR: <strong style={{ color: 'var(--text-secondary)' }}>{c.operating_room}</strong></span>}
                  </div>

                  {/* Team Assignment Status Badges */}
                  {team.length > 0 && (
                    <div style={{ marginBottom: '12px' }}>
                      <div style={{ fontSize: '0.7rem', textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--text-muted)', marginBottom: 6 }}>
                        Team Assignments
                      </div>
                      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                        {team.map((t, i) => (
                          <div key={i} style={{
                            display: 'flex', alignItems: 'center', gap: '6px',
                            padding: '4px 10px', borderRadius: 6,
                            fontSize: '0.78rem', fontWeight: 500,
                            background: t.status === 'confirmed' ? 'color-mix(in srgb, var(--status-ok) 10%, transparent)' :
                                       t.status === 'declined' ? 'color-mix(in srgb, var(--status-danger) 10%, transparent)' :
                                       t.status === 'alerted' ? 'color-mix(in srgb, var(--status-warn) 10%, transparent)' :
                                       'var(--bg-glass)',
                            color: t.status === 'confirmed' ? 'var(--status-ok-text)' :
                                  t.status === 'declined' ? 'var(--status-danger-text)' :
                                  t.status === 'alerted' ? 'var(--status-warn-text)' :
                                  'var(--text-secondary)',
                            border: `1px solid ${t.status === 'confirmed' ? 'color-mix(in srgb, var(--status-ok) 28%, transparent)' :
                                                   t.status === 'declined' ? 'color-mix(in srgb, var(--status-danger) 28%, transparent)' :
                                                   t.status === 'alerted' ? 'color-mix(in srgb, var(--status-warn) 28%, transparent)' :
                                                   'var(--border-glass)'}`,
                          }}>
                            <span style={{ width: 6, height: 6, borderRadius: '50%',
                              background: t.status === 'confirmed' ? 'var(--status-ok)' :
                                         t.status === 'declined' ? 'var(--status-danger)' :
                                         t.status === 'alerted' ? 'var(--status-warn)' : 'var(--text-secondary)',
                            }} />
                            {t.role_name}: {t.user_id}
                            <span style={{ fontSize: '0.7rem' }}>({t.status})</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Action Buttons */}
                  <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                    {/* Alert Team */}
                    {team.length === 0 && !isInProgress && (
                      <button
                        onClick={async () => {
                          setCaseError(null);
                          try {
                            await alertSurgicalTeam(c.id, [
                              { user_id: 'DOC004', role: 'Lead Surgeon' },
                              { user_id: 'anesthetist_1', role: 'Anesthetist' },
                            ]);
                            loadCases();
                          } catch (e) {
                            console.error(e);
                            setCaseError({ id: c.id, message: (e instanceof Error && e.message) || 'The team could not be alerted. Try again.' });
                          }
                        }}
                        style={{
                          padding: '7px 18px', borderRadius: 6, border: 'none', cursor: 'pointer',
                          background: 'var(--status-danger)', color: 'var(--status-on-fill)', fontWeight: 700, fontSize: '0.78rem',
                          fontFamily: 'inherit', transition: 'all 0.2s',
                          display: 'flex', alignItems: 'center', gap: '6px',
                        }}
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>
                        Alert Team
                      </button>
                    )}

                    {/* Start Surgery */}
                    {!isInProgress && team.length > 0 && (
                      <button
                        onClick={async () => {
                          setCaseError(null);
                          try {
                            await startSurgery(c.id);
                            loadCases();
                          } catch (e) {
                            setCaseError({ id: c.id, message: (e instanceof Error && e.message) || 'Cannot start yet, awaiting team confirmations.' });
                          }
                        }}
                        disabled={!allConfirmed || hasAlerted}
                        style={{
                          /* "Awaiting Confirmations" used --text-muted on --bg-glass
                             at opacity 0.5 and measured 1.92:1 in the light theme —
                             the worst number in the whole frontend, on the most
                             consequential control in the OR. The neutral disabled
                             pair holds 6.3:1 and still reads as unavailable. */
                          padding: '7px 18px', borderRadius: 6, border: 'none', cursor: allConfirmed ? 'pointer' : 'not-allowed',
                          background: allConfirmed ? 'var(--accent-deep)' : 'var(--btn-disabled-bg)',
                          color: allConfirmed ? 'var(--btn-primary-text)' : 'var(--btn-disabled-text)',
                          fontWeight: 700, fontSize: '0.78rem', fontFamily: 'inherit',
                          transition: 'all 0.2s',
                          display: 'flex', alignItems: 'center', gap: '6px',
                        }}
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                        {allConfirmed ? 'Start Surgery' : 'Awaiting Confirmations'}
                      </button>
                    )}

                    {/* Complete Surgery */}
                    {isInProgress && (
                      <button
                        onClick={async () => {
                          setCaseError(null);
                          try {
                            await completeSurgery(c.id);
                            loadCases();
                          } catch (e) {
                            console.error(e);
                            setCaseError({ id: c.id, message: (e instanceof Error && e.message) || 'The surgery could not be marked complete. Try again.' });
                          }
                        }}
                        style={{
                          padding: '7px 18px', borderRadius: 6, border: 'none', cursor: 'pointer',
                          background: 'var(--status-ok)', color: 'var(--status-on-fill)',
                          fontWeight: 700, fontSize: '0.78rem', fontFamily: 'inherit',
                          transition: 'all 0.2s',
                          display: 'flex', alignItems: 'center', gap: '6px',
                        }}
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="20 6 9 17 4 12"/></svg>
                        Complete Surgery
                      </button>
                    )}

                    {/* Re-alert (if some declined) */}
                    {team.some((t) => t.status === 'declined') && !isInProgress && (
                      <button
                        onClick={() => loadCases()}
                        style={{
                          padding: '7px 14px', borderRadius: 6, border: '1px solid color-mix(in srgb, var(--status-warn) 28%, transparent)',
                          background: 'transparent', color: 'var(--status-warn-text)', fontWeight: 600, fontSize: '0.75rem',
                          fontFamily: 'inherit', cursor: 'pointer',
                        }}
                      >
                        ↻ Refresh Status
                      </button>
                    )}
                  </div>
                  {caseError && caseError.id === c.id && (
                    <p role="alert" style={{ margin: '8px 0 0', fontSize: '0.8rem', color: 'var(--status-danger-text)' }}>
                      {caseError.message}
                    </p>
                  )}
                </div>
              );
            })
          )}
        </div>
      )}

      {/* ═══ TIER 1 — Week View ═══ */}
      {tier === 1 && activeView === 'schedule' && (
        <div className={`suite__week ${animState === 'collapsing' ? 'suite__week--entering' : ''}`}>
          
          {/* Emergency Banner */}
          {emergencyQueue.length > 0 && (
            <div className="suite__emergency-banner glass-card" style={{ 
              borderColor: 'color-mix(in srgb, var(--status-danger) 28%, transparent)',
              background: 'color-mix(in srgb, var(--status-danger) 10%, transparent)',
              marginBottom: '16px'
            }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '8px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                  <div style={{ width: '8px', height: '8px', borderRadius: '50%', background: 'var(--status-danger)', boxShadow: '0 0 8px var(--status-danger)', animation: 'pulse 1.5s infinite' }} />
                  <span style={{ color: 'var(--status-danger)', fontWeight: 600 }}>{emergencyQueue.length} Emergency Case{emergencyQueue.length > 1 ? 's' : ''} Pending</span>
                </div>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', marginTop: '12px' }}>
                {emergencyQueue.map(s => (
                  <button 
                    key={s.id} 
                    className="suite__surgery-card glass-card" 
                    onClick={() => handleSurgeryClick(s.id)}
                    style={{ borderColor: 'color-mix(in srgb, var(--status-danger) 28%, transparent)' }}
                  >
                    <div className="suite__surgery-time-block">
                      <span className="suite__surgery-time" style={{ color: 'var(--status-danger)', fontWeight: 'bold' }}>URGENT</span>
                      <span className="suite__surgery-duration">{formatDuration(s.estimated_duration_min)}</span>
                    </div>
                    <div className="suite__surgery-line">
                      <span className="suite__surgery-line-dot" style={{ background: 'var(--status-danger)', boxShadow: `0 0 8px color-mix(in srgb, var(--status-danger) 50%, transparent)` }} />
                    </div>
                    <div className="suite__surgery-body">
                      <div className="suite__surgery-top">
                        <h3 className="suite__surgery-proc">{s.procedure_name}</h3>
                        <span className="suite__surgery-ot">{s.status === 'emergency_acknowledged' ? 'Acknowledged' : s.status === 'emergency_ready' ? 'Ready' : 'Pending'}</span>
                      </div>
                      <span className="suite__surgery-diagnosis">{s.diagnosis}</span>
                      <div className="suite__surgery-meta">
                        <span className="suite__surgery-patient">{s.patient_name}{s.bed ? ` · Bed ${s.bed}` : ''}</span>
                        <span className="suite__surgery-risk" style={{ color: 'var(--status-danger)' }}>EMERGENCY</span>
                      </div>
                    </div>
                  </button>
                ))}
              </div>
            </div>
          )}

          {schedule.length === 0 ? (
            <div className="suite__empty glass-card">
              <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" strokeWidth="1" strokeLinecap="round" strokeLinejoin="round">
                <rect x="3" y="4" width="18" height="18" rx="2" ry="2"/>
                <line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/>
                <line x1="3" y1="10" x2="21" y2="10"/>
              </svg>
              <span>No surgeries scheduled this week</span>
            </div>
          ) : (
            schedule.map((day, idx) => (
              <button
                key={day.date}
                className={`suite__day-card glass-card ${day.surgeries.length === 0 ? 'suite__day-card--empty' : ''} ${day.is_today ? 'suite__day-card--today' : ''} ${animState === 'expanding' && selectedDayIdx === idx ? 'suite__day-card--expanding' : ''} ${animState === 'expanding' && selectedDayIdx !== idx ? 'suite__day-card--dismissed' : ''}`}
                onClick={() => handleDayClick(idx)}
                style={{
                  animationDelay: `${idx * 0.06}s`,
                  ...(animState === 'expanding' && selectedDayIdx !== idx ? {
                    '--dismiss-dir': idx < (selectedDayIdx ?? 0) ? '-1' : '1',
                  } as React.CSSProperties : {}),
                }}
                id={`suite-day-${day.date}`}
              >
                <div className="suite__day-header">
                  <span className="suite__day-name">{day.day_label}</span>
                  <span className="suite__day-date">{formatDate(day.date)}</span>
                  {day.is_today && <span className="suite__today-badge">Today</span>}
                </div>
                <div className="suite__day-count">
                  <span className="suite__day-count-num">{day.surgeries.length}</span>
                  <span className="suite__day-count-label">{day.surgeries.length === 1 ? 'Surgery' : 'Surgeries'}</span>
                </div>
                <div className="suite__day-list">
                  {day.surgeries.map((s) => (
                    <div key={s.id} className="suite__day-item">
                      <span className="suite__day-item-time">{formatTime(s.scheduled_time)}</span>
                      <span className="suite__day-item-dot" style={{ background: riskColor(s.risk_level) }} />
                      <div className="suite__day-item-info">
                        <span className="suite__day-item-proc">{s.procedure_name.length > 35 ? s.procedure_name.slice(0, 35) + '…' : s.procedure_name}</span>
                        <span className="suite__day-item-patient">{s.patient_name} · {s.operating_room}</span>
                      </div>
                    </div>
                  ))}
                </div>
                {/* ── Day Summary Footer ── */}
                {day.surgeries.length > 0 && (() => {
                  const totalMins = day.surgeries.reduce((acc, s) => acc + s.estimated_duration_min, 0);
                  // Elevated AND critical cases. The label said "Elevated Risk",
                  // which read as if the critical cases were counted elsewhere.
                  const highRisk = day.surgeries.filter(s => s.risk_level === 'critical' || s.risk_level === 'elevated').length;
                  const activeORs = new Set(day.surgeries.map(s => s.operating_room)).size;
                  return (
                    <div className="suite__day-footer">
                      <div className="suite__day-footer-stat">
                        <span className="suite__day-footer-val">{formatDuration(totalMins)}</span>
                        <span className="suite__day-footer-lbl">Total OR time</span>
                      </div>
                      <div className="suite__day-footer-stat">
                        <span className="suite__day-footer-val">{highRisk}</span>
                        <span className="suite__day-footer-lbl">High risk</span>
                      </div>
                      <div className="suite__day-footer-stat">
                        <span className="suite__day-footer-val">{activeORs}</span>
                        <span className="suite__day-footer-lbl">Active rooms</span>
                      </div>
                    </div>
                  );
                })()}
              </button>
            ))
          )}
        </div>
      )}

      {/* ═══ TIER 2 — Day View ═══ */}
      {tier === 2 && selectedDayIdx !== null && schedule[selectedDayIdx] && (
        <div className={`suite__day-view ${animState === 'detail-in' ? 'suite__day-view--fading' : ''} ${animState === 'detail-out' ? 'suite__day-view--entering' : ''}`}>
          <button className="suite__back" onClick={handleBackToWeek} id="suite-back-week">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M19 12H5M12 19l-7-7 7-7"/>
            </svg>
            <span>Week Overview</span>
          </button>

          <div className="suite__day-view-header">
            <h2 className="suite__day-view-title">
              {schedule[selectedDayIdx].day_label}
              {schedule[selectedDayIdx].is_today && <span className="suite__today-badge">Today</span>}
            </h2>
            <span className="suite__day-view-date">{formatDate(schedule[selectedDayIdx].date)}</span>
            <span className="suite__day-view-count">{schedule[selectedDayIdx].surgeries.length} scheduled procedures</span>
          </div>

          <div className="suite__timeline">
            {schedule[selectedDayIdx].surgeries.map((s, i) => (
              <button
                key={s.id}
                className="suite__surgery-card glass-card"
                onClick={() => handleSurgeryClick(s.id)}
                style={{ animationDelay: `${i * 0.08}s` }}
                id={`suite-surgery-${s.id}`}
              >
                <div className="suite__surgery-time-block">
                  <span className="suite__surgery-time">{formatTime(s.scheduled_time)}</span>
                  <span className="suite__surgery-duration">{formatDuration(s.estimated_duration_min)}</span>
                </div>

                <div className="suite__surgery-line">
                  <span className="suite__surgery-line-dot" style={{ background: riskColor(s.risk_level), boxShadow: `0 0 8px color-mix(in srgb, ${riskColor(s.risk_level)} 25%, transparent)` }} />
                  {i < schedule[selectedDayIdx].surgeries.length - 1 && <span className="suite__surgery-line-stem" />}
                </div>

                <div className="suite__surgery-body">
                  <div className="suite__surgery-top">
                    <h3 className="suite__surgery-proc">{s.procedure_name}</h3>
                    <span className="suite__surgery-ot">{s.operating_room}</span>
                  </div>
                  <span className="suite__surgery-diagnosis">{s.diagnosis}</span>
                  <div className="suite__surgery-meta">
                    <span className="suite__surgery-patient">
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
                      {s.patient_name} · {s.age}y/{s.sex}
                    </span>
                    {s.surgeon_name && (
                      <span className="suite__surgery-surgeon">{s.surgeon_name}</span>
                    )}
                    <span className="suite__surgery-risk" style={{ color: riskColor(s.risk_level) }}>
                      {s.risk_level === 'critical' ? '⬤' : s.risk_level === 'elevated' ? '◉' : '○'} {scheduleBand(s)}
                    </span>
                    {successPct(s.success_probability) !== null && (
                      <span className="suite__surgery-success" style={{ color: successColor(successPct(s.success_probability)) }}>
                        {successPct(s.success_probability)}% success
                      </span>
                    )}
                  </div>
                </div>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* ═══ TIER 3 — Surgery Detail (Dense Compact) ═══ */}
      {tier === 3 && selectedSurgery && (
        <div className={`suite__detail suite__detail--compact ${animState === 'detail-out' ? 'suite__detail--exiting' : ''}`}>
          <div className="suite__detail-top-nav">
            <button className="suite__back" onClick={handleBackToDay} id="suite-back-day">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M19 12H5M12 19l-7-7 7-7"/>
              </svg>
              <span>{selectedDayIdx !== null ? `${schedule[selectedDayIdx]?.day_label} Agenda` : 'Surgical Suite'}</span>
            </button>
            <div className="suite__detail-actions">
              <button 
                className={`suite__btn-prep ${prepSuccess ? 'suite__btn-prep--success' : ''}`}
                onClick={handlePrepForSurgery}
                disabled={prepLoading || prepSuccess}
              >
                {prepLoading ? (
                  <span className="suite__btn-spinner" />
                ) : prepSuccess ? (
                  <>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="20 6 9 17 4 12"/></svg>
                    Alert Sent
                  </>
                ) : (
                  <>
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
                    Prep for Surgery
                  </>
                )}
              </button>
            </div>
          </div>

          {/* ── Patient Bar (Dense) ── */}
          <div className="suite__detail-patient-bar-dense glass-card">
            <div className="suite__patient-row">
              <div className="suite__detail-patient-avatar">{selectedSurgery.patient_name.charAt(0)}</div>
              <div className="suite__detail-patient-info">
                <div className="suite__patient-name-wrap">
                  <span className="suite__detail-patient-name">{selectedSurgery.patient_name}</span>
                  <span className="suite__detail-patient-sub">{selectedSurgery.age}y · {selectedSurgery.sex} · MRN: {selectedSurgery.mrn}{selectedSurgery.bed ? ` · Bed ${selectedSurgery.bed}` : ''}</span>
                </div>
                {/* Comorbidities + allergies integrated into patient header */}
                <div className="suite__pills suite__pills--dense">
                  {selectedSurgery.co_morbidities.map((c, i) => (
                    <span key={i} className="suite__pill suite__pill--dense">{c}</span>
                  ))}
                  {(() => {
                    // Normalize allergies to a list — it can arrive as a parsed array
                    // (detail endpoint) OR a raw JSON string (list/intermediate fetch).
                    // Never silently drop them: a missed allergy is a safety hazard.
                    const raw = selectedSurgery.allergies as unknown
                    let list: string[] = []
                    if (Array.isArray(raw)) list = raw as string[]
                    else if (typeof raw === 'string' && raw.trim()) {
                      try { const p = JSON.parse(raw); list = Array.isArray(p) ? p : [String(p)] }
                      catch { list = raw.split(',').map(s => s.trim()).filter(Boolean) }
                    }
                    return list.length > 0
                      ? list.map((a, i) => (
                          <span key={`al-${i}`} className="suite__pill suite__pill--dense suite__pill--allergy" title="Drug allergy">⚠ {a}</span>
                        ))
                      : (
                          // NOT "NKDA". NKDA is a positive clinical assertion —
                          // "I asked, and there are none". Orb has no basis to make
                          // it: an empty list here means the allergy field was left
                          // blank at admission, which is the default. Say what is
                          // actually true (nothing is on file) and colour it as an
                          // open item, so the anaesthetist asks instead of trusting.
                          <span
                            className="suite__pill suite__pill--dense suite__pill--allergy-unknown"
                            title="No allergy history is on file for this patient. This is NOT a statement that they have no allergies. Confirm with the patient or next of kin before induction."
                          >
                            Allergies not recorded
                          </span>
                        )
                  })()}
                </div>
              </div>
              <div className="suite__header-stats">
                  <div className="suite__detail-meta-pill suite__detail-meta-pill--ot">{selectedSurgery.operating_room}</div>
                  <div className="suite__detail-meta-pill">
                    {formatTime(selectedSurgery.scheduled_time)} ({selectedSurgery.estimated_duration_min}m)
                  </div>
              </div>
            </div>
          </div>

          {/* ── Compact Body Grid ── */}
          <div className="suite__grid-dense">
            
            {/* Left Column */}
            <div className="suite__grid-col">
              
              {/* Clinical Handoff Context */}
              {selectedSurgery.imported_context && (
                <div className="suite__detail-section glass-card" style={{ borderColor: 'var(--accent-gold)' }}>
                  <h4 className="suite__detail-section-title" style={{ color: 'var(--accent-gold)' }}>
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line><polyline points="10 9 9 9 8 9"></polyline></svg>
                    Clinical Handoff Context (Medicine)
                  </h4>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                    {!!selectedSurgery.imported_context.medications?.length && (
                      <div className="suite__handoff-block">
                        <div style={{ fontSize: '0.75rem', textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--text-muted)', marginBottom: '4px' }}>Active Medications</div>
                        {selectedSurgery.imported_context.medications.map((m, i) => (
                          <div key={i} style={{ fontSize: '0.85rem', paddingLeft: '8px', borderLeft: '2px solid var(--accent-gold)' }}>{m}</div>
                        ))}
                      </div>
                    )}
                    {!!selectedSurgery.imported_context.vitals?.length && (
                      <div className="suite__handoff-block">
                        <div style={{ fontSize: '0.75rem', textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--text-muted)', marginBottom: '4px' }}>Recent Vitals</div>
                        {selectedSurgery.imported_context.vitals.map((v, i) => (
                          <div key={i} style={{ fontSize: '0.85rem' }}>{v}</div>
                        ))}
                      </div>
                    )}
                    {!!selectedSurgery.imported_context.recent_labs?.length && (
                      <div className="suite__handoff-block">
                        <div style={{ fontSize: '0.75rem', textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--text-muted)', marginBottom: '4px' }}>Recent Labs</div>
                        {selectedSurgery.imported_context.recent_labs.map((l, i) => (
                          <div key={i} style={{ fontSize: '0.85rem' }}>{l}</div>
                        ))}
                      </div>
                    )}
                    {selectedSurgery.imported_context.scribe_notes && (
                      <div className="suite__handoff-block">
                        <div style={{ fontSize: '0.75rem', textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--text-muted)', marginBottom: '4px' }}>Scribe Notes</div>
                        <div style={{ fontSize: '0.85rem', color: 'var(--text-secondary)', fontStyle: 'italic' }}>
                          "{selectedSurgery.imported_context.scribe_notes}"
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              )}

               {/* Risk Assessment (MedGemma) - Auto expanded/collapsed via click */}
              <div 
                className={`suite__detail-section glass-card suite__risk-section ${riskExpanded ? 'suite__risk-section--expanded' : 'suite__risk-section--limited'}`} 
                onClick={() => setRiskExpanded(!riskExpanded)}
                style={{ cursor: 'pointer' }}
              >
                <div className="suite__detail-section-header">
                  <h4 className="suite__detail-section-title">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>
                    AI Risk Assessment
                    <span className="suite__ai-badge">MedGemma</span>
                    {(() => {
                      const aiStatus = getAiPackageStatus(selectedSurgery)
                      /* 'template' (and 'generated' on older rows) is written by
                         surgery_escalation_service / sage_handoff_service for a
                         DETERMINISTIC prep template: no model call happens on
                         that path. Badging it "AI Generated" fabricated
                         provenance on a clinical surface. */
                      if (aiStatus === 'template' || aiStatus === 'generated') return <span className="suite__enrichment-badge suite__enrichment-badge--neutral">Standard template</span>
                      if (packageUnavailable(aiStatus)) return <span className="suite__enrichment-badge suite__enrichment-badge--fallback">Assessment unavailable</span>
                      return null
                    })()}
                  </h4>
                  <div className="suite__risk-indicator">
                    {selectedSurgery.status.includes('generating') || riskLoading ? <span className="suite__risk-spinner" /> : null}
                    {/* The card's own onClick is a mouse convenience. Collapsed,
                        this section is clipped to 160px, so without a real
                        control the rest of the AI risk assessment was reachable
                        only by pointer — the chevron was a bare <svg>. */}
                    <button
                      type="button"
                      className="suite__risk-toggle"
                      aria-expanded={riskExpanded}
                      aria-label={riskExpanded ? 'Collapse AI risk assessment' : 'Expand AI risk assessment'}
                      onClick={e => { e.stopPropagation(); setRiskExpanded(!riskExpanded) }}
                    >
                      <svg className={`suite__exp-icon ${riskExpanded ? 'suite__exp-icon--up' : ''}`} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden><polyline points="6 9 12 15 18 9"/></svg>
                    </button>
                  </div>
                </div>

                {/* Fallback notification banner */}
                {packageUnavailable(getAiPackageStatus(selectedSurgery)) && (
                  <div className="suite__fallback-label" role="status">
                    Assessment unavailable. Standard prep steps are shown; complete the risk assessment clinically.
                  </div>
                )}
                
                <div className="suite__risk-content-wrap">
                  {selectedSurgery.status.includes('generating') ? (
                    <div className="suite__risk-loading">
                      <span style={{ fontSize: '13px', color: 'var(--text-muted)' }}>Preparing the case...</span>
                      <div className="suite__risk-shimmer" style={{ marginTop: '8px' }} />
                      <div className="suite__risk-shimmer" style={{ width: '80%' }} />
                      <div className="suite__risk-shimmer" style={{ width: '60%' }} />
                    </div>
                  ) : riskLoading && !riskText ? (
                    <div className="suite__risk-loading">
                      <div className="suite__risk-shimmer" />
                      <div className="suite__risk-shimmer" style={{ width: '80%' }} />
                      <div className="suite__risk-shimmer" style={{ width: '60%' }} />
                    </div>
                  ) : (
                    <div className="suite__risk-content">
                      {(riskText || selectedSurgery.risk_assessment || '')
                        .replace(/ {2,}/g, ' ')
                        .split('\n')
                        .map((line, i) => {
                        let l = line.trim()
                        if (!l) return null
                        
                        // Strip all asterisks for a clean premium look
                        l = l.replace(/\*/g, '')

                        // Check if it's a heading (like "ASA Physical Status Classification:")
                        // Or if it's a numbered list item
                        const numMatch = l.match(/^(\d+)\.\s*(.*)/)
                        if (numMatch) {
                          const num = numMatch[1]
                          const rest = numMatch[2]
                          
                          // Make the key text bold up to the colon if it exists
                          const colonIdx = rest.indexOf(':')
                          if (colonIdx > 0 && colonIdx < 60) {
                            return (
                              <div key={i} className="suite__risk-item">
                                <span className="suite__risk-num">{num}.</span>
                                <div className="suite__risk-item-content">
                                  <strong>{rest.substring(0, colonIdx + 1)}</strong>
                                  <span>{rest.substring(colonIdx + 1)}</span>
                                </div>
                              </div>
                            )
                          }
                          return (
                            <div key={i} className="suite__risk-item">
                              <span className="suite__risk-num">{num}.</span>
                              <div className="suite__risk-item-content">{rest}</div>
                            </div>
                          )
                        }

                        if (l.startsWith('-') || l.startsWith('•')) {
                          return (
                            <div key={i} className="suite__risk-bullet-item">
                              <span className="suite__risk-bullet-icon">✦</span>
                              <div className="suite__risk-item-content">{l.replace(/^[-•]\s*/, '')}</div>
                            </div>
                          )
                        }

                        // If it ends with a colon, treat it as a heading
                        if (l.endsWith(':')) {
                          return <h5 key={i} className="suite__risk-heading">{l}</h5>
                        }

                        return <p key={i} className="suite__risk-text">{l}</p>
                      })}
                      {riskLoading && <span className="suite__risk-cursor" />}
                    </div>
                  )}
                </div>
                {!riskExpanded && <div className="suite__risk-mask" />}
              </div>

               {/* Complications */}
              <div className="suite__detail-section glass-card">
                <h4 className="suite__detail-section-title">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
                  Possible Complications
                </h4>
                <div className="suite__complications suite__complications--dense">
                  {selectedSurgery.possible_complications.map((c, i) => (
                    <div key={i} className="suite__complication-item">
                      <span className="suite__complication-dot" />
                      <span>{c}</span>
                    </div>
                  ))}
                </div>
              </div>

              {/* Surgical Team Detailed */}
              <div className="suite__detail-section glass-card">
                <h4 className="suite__detail-section-title">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87"/><path d="M16 3.13a4 4 0 010 7.75"/></svg>
                  Surgical Team
                </h4>
                <div className="suite__staff-list suite__staff-list--dense">
                  {selectedSurgery.staff_required.map((s, i) => (
                    <div key={i} className="suite__staff-card suite__staff-card--dense">
                      <span className="suite__staff-avatar">{s.name.charAt(0)}</span>
                      <div className="suite__staff-info">
                        <span className="suite__staff-name">{s.name}</span>
                        <span className="suite__staff-role">{s.role}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>

            </div>

            {/* Right Column */}
            <div className="suite__grid-col">
              {/* WHO Surgical Safety Checklist — the interactive OR safety record */}
              <WhoSurgicalChecklist surgeryId={selectedSurgery.id} />

              {/* Procedure prep steps (static package content — distinct from the WHO safety checklist) */}
              <div className="suite__detail-section glass-card">
                <div className="suite__section-header-row">
                  <h4 className="suite__detail-section-title">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/></svg>
                    Procedure Prep Steps ({selectedSurgery.surgical_checklist.length})
                  </h4>
                  {/* No estimate, no gauge: an empty ring reading "—%" looked
                      like a measured zero. */}
                  {successPct(selectedSurgery.success_probability) !== null && (
                    <div className="suite__detail-success-gauge suite__detail-success-gauge--micro">
                      <svg viewBox="0 0 36 36" className="suite__gauge-svg">
                        <path className="suite__gauge-bg" d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831" />
                        <path className="suite__gauge-fill" d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831"
                          style={{
                            strokeDasharray: `${successPct(selectedSurgery.success_probability) ?? 0}, 100`,
                            stroke: successColor(successPct(selectedSurgery.success_probability)),
                          }}
                        />
                      </svg>
                      <span className="suite__gauge-value-micro">
                        {successPct(selectedSurgery.success_probability)}%
                      </span>
                    </div>
                  )}
                </div>
                <div className="suite__checklist suite__checklist--dense">
                  {selectedSurgery.surgical_checklist.map((step, i) => (
                    <div key={i} className="suite__checklist-item suite__checklist-item--dense">
                      <span className="suite__checklist-num">{String(i + 1).padStart(2, '0')}</span>
                      <span className="suite__checklist-text">{step}</span>
                    </div>
                  ))}
                </div>
              </div>

              {/* Instruments */}
              <div className="suite__detail-section glass-card">
                <h4 className="suite__detail-section-title">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"><path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z"/></svg>
                  Instruments Required
                </h4>
                <div className="suite__instrument-grid suite__instrument-grid--dense">
                  {selectedSurgery.instruments_required.map((inst, i) => (
                    <span key={i} className="suite__instrument suite__instrument--dense">{inst}</span>
                  ))}
                </div>
              </div>
            </div>

          </div>
        </div>
      )}

      {surgeryLoading && (
        <div className="suite__overlay">
          <div className="suite__overlay-spinner" />
        </div>
      )}

      {intakePopupData && (() => {
        // Find full data if it exists in queue
        const caseData = emergencyQueue.find(s => s.id === intakePopupData.surgeryId)
        return (
          <EmergencyIntakePopup 
            surgeryId={intakePopupData.surgeryId}
            patientName={caseData?.patient_name || 'Incoming Emergency'}
            procedure={caseData?.procedure_name || 'Loading...'}
            indication={caseData?.diagnosis || 'Escalated from Relay'}
            onClose={() => setIntakePopupData(null)}
            onOpenCase={() => {
              setIntakePopupData(null)
              handleSurgeryClick(intakePopupData.surgeryId)
            }}
          />
        )
      })()}

    </div>
  )
}
