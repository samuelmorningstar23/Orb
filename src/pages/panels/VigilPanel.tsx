import { useState, useEffect, useRef, useMemo, useCallback, memo, lazy, Suspense } from 'react'
import { createPortal } from 'react-dom'
import { apiFetch, getPatientDetail, dischargePatient, getOpenEscalations, resolveEscalation, type EscalationChain, addClinicalNote, addMedication, getAdministrations, givenAcrossAllergy, isSafetyAckRequired, getPatientForecast, getTimelineProjection, getDischargeQrObjectUrl, prefetchSageContext, getUnseenCounts, socket, getUser, buildHeaders, type Patient, type MedicationSafety, type SafetyAck, type AdministrationRecord } from '../../api/client'
import { ClinicalCourseTimeline } from '../../components/ClinicalCourseTimeline'
import { useActivePatient } from '../../contexts/ActivePatientContext'
import { useActiveView } from '../../contexts/ActiveViewContext'
import { onOpenPatient } from '../../lib/orbBus'
import { parseUTC, formatUTCDate, formatUTCDateTime, formatUTCTime, formatClinicalDateTime, parseDateSearch, matchesDateSearch } from '../../lib/datetime'
import { sexLabel } from '../../lib/demographics'
import '../Vigil.css'
import CareBundleTimer from '../../components/vigil/CareBundleTimer'
import SafetyAlertModal from '../../components/vigil/SafetyAlertModal'
import AdministerModal, { type AdministerAllergy } from '../../components/vigil/AdministerModal'
import PatientStory from '../../components/vigil/PatientStory'
import SageInsightsFeed from '../../components/sage/SageInsightsFeed'
import SageAlertAssessment from '../../components/sage/SageAlertAssessment'
import News2Drivers, { chartDriverView } from '../../components/vigil/News2Drivers'
import News2Badge from '../../components/News2Badge'
import type { News2 } from '../../api/nurse'
import WardBriefCard from '../../components/sage/WardBrief'
import { DischargePrintView } from '../../components/DischargePrintView'
import { InpatientRecordPrintView, type RecordNote } from '../../components/InpatientRecordPrintView'
import { getDischargeAdvisory, type DischargeAdvisory } from '../../api/billing'
import { LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip as RechartsTooltip, Legend, ReferenceArea, ResponsiveContainer } from 'recharts'
import SizedChart from '../../components/SizedChart'
import Icon from '../../components/common/Icon'
import { useChartTheme } from '../../components/charts/useChartTheme'
import { useVisiblePolling } from '../../hooks/useVisiblePolling'
import { useOfflineSnapshot } from '../../hooks/useOfflineSnapshot'
import { assertOnlineForWrite } from '../../lib/offline/writeGuard'
import OfflineBanner from '../../components/OfflineBanner'
import { useFocusTrap } from '../../hooks/useFocusTrap'
import { observedAt, observedBefore, vitalTemperature, vitalWhole } from '../../lib/vitals'
import { breakdownRowFor, news2Description, type News2BreakdownRow, type News2ScoredVital } from '../../lib/news2'
import { vitalTrendWindow, trendSpanShort, trendAxisLabel, trendAriaPhrase, type VitalTrendWindow } from '../../lib/vitalTrend'
import { buildLabTrends, labGraphData, labTrendLabel, labTrendTone, LAB_TREND_TONE_COLOR, type LabStatus, type LabTrend } from '../../lib/labTrends'
import { hasPermission, OUTCOME_RECORD, PATIENT_WRITE, DISCHARGE_GENERATE, DISCHARGE_PUBLISH } from '../../lib/roles'
// COMPOSED, never copied: this is the same component the charge nurse uses, so
// the two recorders can never drift into writing two different ground truths.
import OutcomeCaptureSheet from './nurse/OutcomeCaptureSheet'

// Lazily loaded so the print-only Medication Administration Record bundle is not
// pulled into the main Vigil chunk until a clinician actually prints meds.
const MedListPrintView = lazy(() => import('../../components/print/MedListPrintView'))

/* ─── Paginated patient-board fetch ───
   getPatients() in api/client returns only the array (it discards headers), but
   the board now needs the X-Total-Count header to drive "Load more". This helper
   does the raw fetch here — mirroring the centralized auth (Bearer) + 401 →
   orb:session-expired contract used by apiFetch — and returns both the page rows
   and the total match count. Default page size matches the endpoint's default. */
const VIGIL_PAGE_SIZE = 100

/* ─── House census (bed occupancy) ───
   THE DENOMINATOR IS NEVER A LITERAL IN THIS FILE.

   The Bed Occupancy tile used to render `admitted / 35` with 35 written into the
   JSX. The site's real capacity is 90 (six wards x ORB_WARD_BEDS), so with 30
   patients admitted the ward board announced 86% occupancy against a true 33% —
   wrong by a factor of two and a half, on the screen a bed manager would use to
   decide whether the hospital can accept the next ambulance. A bed-occupancy
   figure that wrong is worse than no figure, because no figure sends somebody to
   ask and a wrong one does not.

   Capacity is a SITE SETTING (forecast_service.WARD_CAPACITY, discovered per
   deployment at startup), so the only honest way to show it on a third screen is
   to read the same server-computed census the Command Center KPI and the
   capacity forecast already agree on. This takes the percentage VERBATIM from
   `census.occupancy_pct` rather than dividing anything here: two screens that
   each do their own arithmetic over the same inputs are two screens that will
   eventually disagree by a rounding rule.

   POLLED SLOWLY AND ONLY FOR THE TILE THAT NEEDS IT. /command-center/ assembles
   the whole house snapshot (including a forecast pass over every admitted
   patient); putting it on the board's 30s vitals poll would be a heavy aggregate
   query firing on every ward tablet forever. */
const CENSUS_REFRESH_MS = 120_000

/* The honesty label on a simulator's result (source 'simulated', written by
   the lab and imaging bots). Same look as the "Simulated vitals" chip. */
function SimulatedBadge({ what = 'result' }: { what?: string }) {
  return (
    <span
      role="note"
      title={`This ${what} was generated by the simulator. It is not from this patient.`}
      style={{ display: 'inline-flex', alignItems: 'center', padding: '1px 8px', borderRadius: 999, fontSize: '0.68rem', fontWeight: 600, color: 'var(--status-warn-text)', border: '1px solid var(--status-warn)', background: 'var(--bg-glass)', whiteSpace: 'nowrap' }}
    >
      Simulated
    </span>
  )
}

/** Fill tokens for the tile's edge and the plotted line. */
const LAB_STATUS_FILL: Record<LabStatus, string> = {
  critical: 'var(--status-danger)', high: 'var(--status-warn)', abnormal: 'var(--status-warn)',
  low: 'var(--status-info)', normal: 'var(--status-ok)', unranged: 'var(--border-subtle)',
}
/** Text tokens for a printed value. A result with no range and no flag is
 *  printed plain: there is nothing to colour it by. */
const LAB_STATUS_TEXT: Record<LabStatus, string> = {
  critical: 'var(--status-danger-text)', high: 'var(--status-warn-text)', abnormal: 'var(--status-warn-text)',
  low: 'var(--status-info-text)', normal: 'var(--status-ok-text)', unranged: 'var(--text-primary)',
}

function labRangeText(lab: Pick<LabTrend, 'refLow' | 'refHigh' | 'refText'>): string {
  if (lab.refLow !== null && lab.refHigh !== null) return `${lab.refLow}–${lab.refHigh}`
  return lab.refText || 'No reference range'
}

/** The orders the patient is on now: active and as-needed. Discontinued
 *  orders were printed as a discharge prescription before. */
function currentMedications(meds: unknown): any[] {
  return (Array.isArray(meds) ? meds : []).filter((m: any) => m?.status === 'active' || m?.status === 'prn')
}

/** Notes for the printed record: charted documents (lab results are not
 *  notes) and Scribe notes a clinician has signed, oldest first. */
function signedNotes(docs: unknown, scribe: unknown): RecordNote[] {
  const out: (RecordNote & { t: number })[] = []
  for (const d of Array.isArray(docs) ? docs : []) {
    if (!d || d.doc_type === 'lab_result' || typeof d.content !== 'string') continue
    out.push({ title: d.title || d.doc_type || 'Note', at: d.created_at, text: d.content,
      t: d.created_at ? parseUTC(d.created_at).getTime() : 0 })
  }
  for (const n of Array.isArray(scribe) ? scribe : []) {
    if (!n || n.status !== 'signed') continue
    const text = n.structured_note || n.raw_transcript
    if (typeof text !== 'string' || !text.trim()) continue
    const at = n.signed_at || n.created_at
    out.push({ title: `Scribe note (${n.note_format || 'note'}), signed`, at, text,
      t: at ? parseUTC(at).getTime() : 0 })
  }
  return out.sort((a, b) => a.t - b.t).map(n => ({ title: n.title, at: n.at, text: n.text }))
}

interface HouseCensus {
  total_admitted: number
  total_capacity: number
  occupancy_pct: number
}

async function fetchPatientsPage(
  offset: number,
  limit: number = VIGIL_PAGE_SIZE,
): Promise<{ items: Patient[]; total: number }> {
  const res = await fetch(
    `${window.location.origin}/api/vigil/patients?limit=${limit}&offset=${offset}`,
    { credentials: 'include', headers: buildHeaders('GET') },
  )
  if (!res.ok) {
    if (res.status === 401) window.dispatchEvent(new CustomEvent('orb:session-expired'))
    throw new Error(`Failed to load patients: ${res.status}`)
  }
  const items = (await res.json()) as Patient[]
  const parsed = parseInt(res.headers.get('X-Total-Count') || '', 10)
  return { items, total: Number.isNaN(parsed) ? items.length : parsed }
}





/* ═══════════════════════════════════════════════════
   VIGIL PANEL — with patient detail view
   ═══════════════════════════════════════════════════ */

export default function VigilPanel({ role }: { role?: string | null }) {
  const [livePatients, setPatients] = useState<Patient[]>([])
  const [total, setTotal] = useState(0)
  const [loadingMore, setLoadingMore] = useState(false)
  const [loading, setLoading] = useState(true)
  // Last board-load failure. An empty board and a board we simply could not
  // reach look identical ("0 Critical"), so the failure has to be rendered —
  // a silent zero here reads as "no deteriorating patients".
  const [loadError, setLoadError] = useState<string | null>(null)
  const [selectedPatientId, setSelectedPatientId] = useState<string | null>(null)
  const [sortBy, setSortBy] = useState<'stability' | 'recent'>('stability')
  const [outcomeCaptureOpen, setOutcomeCaptureOpen] = useState(false)
  const canRecordOutcome = hasPermission(OUTCOME_RECORD, role)

  // Declared above the offline snapshot, whose reconnect handler calls it.
  const loadPatients = async () => {
    try {
      const { items, total: t } = await fetchPatientsPage(0, VIGIL_PAGE_SIZE)
      setTotal(t)
      setPatients(prev => {
        // First load (or board smaller than a page): just take the page.
        if (prev.length <= VIGIL_PAGE_SIZE) return items
        // Poll refresh: replace page-one rows with the fresh page, keep any
        // additionally loaded rows (dedup by id so nothing doubles up).
        const firstIds = new Set(items.map(p => p.id))
        const rest = prev.slice(VIGIL_PAGE_SIZE).filter(p => !firstIds.has(p.id))
        return [...items, ...rest]
      })
      setLoadError(null)
    } catch (err) {
      console.error('Failed to load patients:', err)
      setLoadError(err instanceof Error ? err.message : 'The patient board could not be loaded.')
    } finally {
      setLoading(false)
    }
  }

  // Offline copy of the ward board. `patients` below is the SNAPSHOT, not the
  // raw state, so nothing downstream can render a cached board without the
  // banner that dates it. Live data is passed only once a load has actually
  // succeeded — passing the empty initial array through a failed first load
  // would overwrite a good copy on the device with an empty ward.
  const boardOffline = useOfflineSnapshot('vigil:patients', loading || loadError ? null : livePatients, {
    liveFailed: !!loadError,
    onReconnect: () => loadPatients(),
  })
  const patients = boardOffline.data ?? []

  // Stable row-select callback so PatientRow's memo() bails on unchanged rows
  // (a fresh per-row closure would re-render all ~100 rows every vitals tick).
  // Opening a chart also warms Sage's context (speculative prefetch) so the
  // clinician's first question streams faster.
  const handleSelectPatient = useCallback((id: string) => {
    setSelectedPatientId(id)
    prefetchSageContext(id).catch(() => {})
  }, [])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger: loadPatients sets state only after its fetch resolves.
  useEffect(() => { loadPatients() }, [])

  /* "N new since you last looked" per bed.
     Fetched SEPARATELY from the board and deliberately NOT on the 30s vitals
     poll: the endpoint writes one audited PHI-read row per call, and a per-tick
     encrypted-SQLite write is the exact shape behind the July loop stalls. Once
     on mount, and again each time a chart is closed — both user-driven, both
     rare. A failure is swallowed to `{}`: no badge is the correct degraded
     state, and a board that refuses to render because a convenience marker
     could not load would be a far worse trade. */
  const [unseen, setUnseen] = useState<Record<string, number>>({})
  const loadUnseen = useCallback(() => {
    getUnseenCounts().then(setUnseen).catch(() => setUnseen({}))
  }, [])
  useEffect(() => { loadUnseen() }, [loadUnseen])
  useEffect(() => { if (selectedPatientId === null) loadUnseen() }, [selectedPatientId, loadUnseen])

  // Jump-to-patient via the navigation bus: any surface (command palette,
  // Sage, deep links) can open a patient chart via orb:open-patient
  // (ClinicianShell has already switched to the vigil tab). Reuses the exact
  // row-click state path — the detail view fetches by id, so a patient outside
  // the loaded page still opens. Opening a chart also warms Sage's context so
  // the first question streams faster.
  useEffect(() => {
    return onOpenPatient((patientId) => {
      setSelectedPatientId(patientId)
      prefetchSageContext(patientId).catch(() => {})
    })
  }, [])

  /* House-wide census for the Bed Occupancy tile — see HouseCensus above.
     Fetched only for the role that is shown the tile, so no other clinician's
     tablet pays for an aggregate they never see. `null` means "we do not know",
     and the tile renders unknown rather than inventing a denominator. */
  const showOccupancy = role === 'admin'
  const [census, setCensus] = useState<HouseCensus | null>(null)
  const loadCensus = useCallback(() => {
    if (!showOccupancy) return
    apiFetch<{ census?: Partial<HouseCensus> }>('/command-center/')
      .then(snap => {
        const c = snap?.census
        // A capacity of 0 or a missing figure is NOT 0% occupancy — it is a site
        // whose bed count Orb has not been told. Held as null so the tile says so.
        if (c && typeof c.total_capacity === 'number' && c.total_capacity > 0
            && typeof c.occupancy_pct === 'number') {
          setCensus({
            total_admitted: Number(c.total_admitted) || 0,
            total_capacity: c.total_capacity,
            occupancy_pct: c.occupancy_pct,
          })
        } else {
          setCensus(null)
        }
      })
      .catch(() => setCensus(null))
  }, [showOccupancy])
  useEffect(() => { loadCensus() }, [loadCensus])
  useVisiblePolling(loadCensus, CENSUS_REFRESH_MS, showOccupancy)

  /* Live ticks, batched PER PATIENT. The old handler kept ONE 2 s throttle for
     the whole ward, so a tick for one bed swallowed every other bed's tick in
     the same window and most rows only moved on the 30 s poll. Each patient's
     latest tick now waits in `pending`, and the whole batch lands in one render
     at most every BOARD_TICK_MS: nothing is dropped, and the board still
     re-renders no more often than it did. */
  useEffect(() => {
    let pending = new Map<string, BoardTick>()
    let timer: ReturnType<typeof setTimeout> | null = null
    let lastFlush = 0
    const flush = () => {
      timer = null
      lastFlush = Date.now()
      const batch = pending
      pending = new Map()
      if (batch.size === 0) return
      setPatients(prev => prev.map(p => {
        const t = batch.get(p.id)
        return t ? applyBoardTick(p, t) : p
      }))
    }
    const handleVitalsUpdate = (tick: BoardTick) => {
      if (!tick?.patient_id) return
      // Two ticks for one patient in a window: the newer observation waits,
      // whichever arrived last.
      const held = pending.get(tick.patient_id)
      if (held && observedBefore(tick, held)) return
      pending.set(tick.patient_id, tick)
      if (timer) return
      const wait = BOARD_TICK_MS - (Date.now() - lastFlush)
      if (wait <= 0) flush()
      else timer = setTimeout(flush, wait)
    }
    socket.on('vitals:update', handleVitalsUpdate)
    return () => {
      socket.off('vitals:update', handleVitalsUpdate)
      if (timer) clearTimeout(timer)
    }
  }, [])

  // 30s poll refreshes page one only (the highest-risk top of the board); any
  // additionally loaded pages are preserved (see loadPatients merge). Paused
  // automatically while the tab is backgrounded (useVisiblePolling).
  useVisiblePolling(loadPatients, 30000)

  const loadMore = async () => {
    if (loadingMore) return
    setLoadingMore(true)
    try {
      const { items, total: t } = await fetchPatientsPage(patients.length, VIGIL_PAGE_SIZE)
      setTotal(t)
      setPatients(prev => {
        const ids = new Set(prev.map(p => p.id))
        return [...prev, ...items.filter(p => !ids.has(p.id))]
      })
    } catch (err) {
      console.error('Failed to load more patients:', err)
    } finally {
      setLoadingMore(false)
    }
  }

  const sortedPatients = useMemo(() => {
    const sorted = [...patients];
    if (sortBy === 'stability') {
      sorted.sort(boardOrder)
    } else {
      sorted.sort((a, b) => 
        new Date(b.admission_date || 0).getTime() - new Date(a.admission_date || 0).getTime()
      )
    }
    
    // Float genuinely critical patients to the top regardless of sort — driven
    // by the real NEWS2 band, not a hardcoded demo patient id (the previous
    // build pinned 'PAT007' as a permanent fake emergency).
    sorted.sort((a, b) => {
      const aEmerg = a.risk_level === 'critical'
      const bEmerg = b.risk_level === 'critical'
      if (aEmerg && !bEmerg) return -1
      if (!aEmerg && bEmerg) return 1
      return 0
    })
    return sorted;
  }, [patients, sortBy])

  if (selectedPatientId) {
    return (
      /* KEYED BY PATIENT. Without the key, an orb:open-patient event (Sage's
         ward-brief watchlist, the command palette, a deep link) swapped the
         prop under a live PatientDetailView instead of remounting it, and every
         piece of state it holds carried across the switch: the previous
         patient's in-flight chart/eMAR responses landed in the new patient's
         view, and any modal left open — Add Medication, Discharge — stayed open
         and then acted on the NEW patient id. A chart is a chart: a different
         patient is a different component. */
      <PatientDetailView
        key={selectedPatientId}
        patientId={selectedPatientId}
        role={role}
        onBack={() => { setSelectedPatientId(null); loadPatients() }}
      />
    )
  }

  // Admitted reflects the full server-side match count; elevated/critical are
  // tallied over the loaded rows (the board only knows risk for what it has).
  const admitted = total || patients.length
  const elevated = patients.filter(p => p.risk_level === 'elevated').length
  const critical = patients.filter(p => p.risk_level === 'critical').length
  // Nothing loaded AND the last attempt failed: the counts below are not "0
  // critical patients", they are "unknown". Render them as unknown.
  const boardUnavailable = !!loadError && patients.length === 0
  const metric = (n: number | string) => (boardUnavailable ? '—' : n)

  return (
    <div className="orb-panel animate-fade-in">
      <div className="orb-panel-grid">
        <div className="orb-metric-card glass-card">
          <span className="orb-metric-value metric-mono">{metric(admitted)}</span>
          <span className="orb-metric-label" title="Patients admitted under your department">Admitted · My Dept</span>
        </div>
        <div className="orb-metric-card glass-card">
          <span className="orb-metric-value orb-metric-value--warn metric-mono">{metric(elevated)}</span>
          <span className="orb-metric-label">Elevated Risk</span>
        </div>
        <div className="orb-metric-card glass-card">
          <span className="orb-metric-value orb-metric-value--danger metric-mono">{metric(critical)}</span>
          <span className="orb-metric-label">Critical</span>
        </div>
        {showOccupancy && (
          <div className="orb-metric-card glass-card">
            {/* The percentage is the SERVER'S, copied not recomputed, so this
                tile and the Command Center KPI cannot drift. Unknown capacity
                renders as an em dash — never as a percentage of a number Orb
                made up. Note this one is HOUSE-WIDE while the tile at the far
                left is department-scoped; the labels say which is which. */}
            <span className="orb-metric-value metric-mono">
              {census ? `${census.occupancy_pct}%` : '—'}
            </span>
            <span
              className="orb-metric-label"
              title={census
                ? `${census.total_admitted} of ${census.total_capacity} beds occupied, hospital-wide, the same census the Command Center and the capacity forecast use`
                : 'Bed capacity is a site setting Orb has not been given (ORB_WARD_BEDS). Occupancy cannot be computed without it.'}
            >
              {census
                ? `Bed Occupancy · ${census.total_admitted}/${census.total_capacity}`
                : 'Bed Occupancy · capacity unknown'}
            </span>
          </div>
        )}
      </div>

      <WardBriefCard />

      <SageInsightsFeed />

      <div className="orb-section">
        <div className="orb-section-header">
          <h2 className="orb-section-title">Active Patients</h2>
          <div className="vigil-board-actions">
            {/* THE DOCTOR'S ROUTE TO THE PILOT'S GROUND TRUTH.
                A doctor holds OUTCOME_RECORD (server/permissions.py) for the
                site that names the ward doctor as the shift's one recorder, but
                the capture screen only ever existed inside the nurse panel,
                which a doctor's tab list does not contain — a granted
                permission with no way to exercise it. The ward board is the
                seam: it is the doctor's landing tab and the surface they are on
                at the end of a shift. Gated on the permission mirror, so a role
                the server would refuse never sees the control. */}
            {canRecordOutcome && (
              <button
                type="button"
                className="vigil-outcome-entry"
                onClick={() => setOutcomeCaptureOpen(true)}
                title="Record a deterioration event, or confirm a shift with none"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><path d="M9 15l2 2 4-4"/>
                </svg>
                Shift outcomes
              </button>
            )}
            <div className="relay-chat__sort-controls">
              <button
                className={`sort-btn ${sortBy === 'stability' ? 'sort-btn--active' : ''}`}
                onClick={() => setSortBy('stability')}
                title="Sort by Risk Stability"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
                Stability
              </button>
              <button
                className={`sort-btn ${sortBy === 'recent' ? 'sort-btn--active' : ''}`}
                onClick={() => setSortBy('recent')}
                title="Sort by Recently Added"
              >
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
                Recent
              </button>
            </div>
          </div>
        </div>
        {/* A refresh that failed on top of already-loaded rows keeps the rows
            (stale data beats a blank board) but must say they are stale. This
            replaces an earlier note that said "showing the last successful
            update" without saying WHEN that was — which is the one fact that
            decides whether the rows are still worth acting on. */}
        <OfflineBanner snapshot={boardOffline} what="patient board" onRetry={loadPatients} />
        <div className="vigil-patients-list">
          {loading ? (
            <div className="orb-panel-loading">Loading patients…</div>
          ) : boardUnavailable ? (
            <div className="orb-panel-loading" role="alert">
              <div style={{ color: 'var(--status-danger)', fontWeight: 600 }}>The patient board could not be loaded.</div>
              <div style={{ marginTop: 4, fontSize: '0.85rem', color: 'var(--text-muted)' }}>
                This is a load failure, not an empty ward. Patient risk is unknown until it succeeds.
              </div>
              <button
                onClick={loadPatients}
                style={{ marginTop: 10, padding: '6px 14px', borderRadius: 6, border: '1px solid var(--border-glass)', background: 'var(--btn-gradient)', color: 'var(--btn-primary-text)', fontWeight: 600, cursor: 'pointer' }}
              >
                Retry
              </button>
            </div>
          ) : (
            sortedPatients.map(p => (
              // handleSelectPatient is a stable useCallback identity, so passing
              // it directly (rather than a fresh closure) lets PatientRow's memo
              // bail; it also prefetches Sage context for the opened chart.
              <PatientRow key={p.id} patient={p} onSelect={handleSelectPatient} newSinceReview={unseen[p.id] ?? 0} />
            ))
          )}
        </div>
        {!loading && patients.length < total && (
          <button
            className="vigil-load-more"
            onClick={loadMore}
            disabled={loadingMore}
          >
            {loadingMore ? 'Loading…' : `Load more (${patients.length} of ${total})`}
          </button>
        )}
      </div>

      {/* THE BLINDING BOUNDARY TRAVELS WITH THE COMPONENT. The recorder must
          never see scorecard numbers (server/permissions.py: `outcome.record`
          cannot read the scorecard), and this modal holds nothing but the sheet
          — no metrics, no rates, no adjudication queue — so the doctor's route
          in is blind for exactly the same reason the nurse's is: there is one
          capture surface, and it has never been able to read the score. */}
      {outcomeCaptureOpen && (
        <ModalOverlay onClose={() => setOutcomeCaptureOpen(false)}>
          <div
            className="modal glass-card"
            role="dialog"
            aria-modal="true"
            aria-labelledby="vigil-outcome-modal-title"
            style={{ maxWidth: 760, width: '100%' }}
            onClick={e => e.stopPropagation()}
          >
            <h2 className="modal__title" id="vigil-outcome-modal-title">End-of-shift outcomes</h2>
            <div className="modal__body" style={{ maxHeight: '75vh', overflowY: 'auto' }}>
              {/* The board's rows carry `id`; the picker's contract is
                  `patient_id`. Mapped here rather than reshaping the sheet, so
                  the nurse's board keeps passing its own cards unchanged. */}
              <OutcomeCaptureSheet patients={patients.map(p => ({ patient_id: p.id, name: p.name }))} />
            </div>
            <div className="modal__actions">
              <button
                className="modal__btn modal__btn--ghost"
                style={{ padding: '8px 16px', borderRadius: 8 }}
                onClick={() => setOutcomeCaptureOpen(false)}
              >
                Close
              </button>
            </div>
          </div>
        </ModalOverlay>
      )}
    </div>
  )
}

// Emergency emphasis is driven by the real NEWS2 band (critical), not a
// hardcoded demo patient id — the sickest patients read at a glance across the
// board because they genuinely are, not because they were pinned.
/* Panel modals must escape .shell__main's isolated stacking context (z-index 2,
   which sits under the z-10 sidebar and root-level widgets) — otherwise their
   scrim dims only part of the chrome. Portal to <body>, exactly like
   SafetyAlertModal / AdministerModal / PatientStory already do.

   The overlay also owns the dialog keyboard contract for every modal in this
   panel: useFocusTrap keeps Tab inside the modal (so a clinician cannot tab
   onto a chart control hidden behind the scrim and press Enter on it), closes
   on Escape, restores focus to the trigger on unmount, and locks body scroll.
   Each child still declares role="dialog"/aria-modal on its own .modal box so
   the boundary is announced. Passing `onClose` as undefined (mid-submit, say)
   disables Escape without losing the trap. */
function ModalOverlay({ onClose, children }: { onClose?: () => void; children: React.ReactNode }) {
  const ref = useFocusTrap<HTMLDivElement>(onClose)
  return createPortal(
    <div
      className="modal-overlay"
      ref={ref}
      /* Backdrop click closes; a click that started inside the modal box does
         not (children also stopPropagation, this is the belt to that braces). */
      onClick={e => { if (e.target === e.currentTarget) onClose?.() }}
    >{children}</div>,
    document.body,
  )
}

const isEmergency = (p: Patient) => p.risk_level === 'critical';

/** A `vitals:update` payload as the board reads it. `risk` is the same
 *  score_vitals_row dict the list scored (bot_service, ingestion, nurse obs). */
interface BoardTick {
  patient_id?: string
  timestamp?: string
  risk?: News2 | null
}

/** Board ticks land in one batch at most this often (see the handler). */
const BOARD_TICK_MS = 2000

/* One live tick onto one board row. The row takes the whole score, so the
   number, the band, the drivers and the sort all move together. `??`, never
   `||`: an index of 0 is a real value, and `||` kept the stale score whenever
   a patient recovered to NEWS2 0. A tick with no aggregate leaves the row. */
function applyBoardTick(p: Patient, tick: BoardTick): Patient {
  const risk = tick.risk
  if (!risk || typeof risk.raw_news2 !== 'number' || !Number.isFinite(risk.raw_news2)) return p
  // Strictly older than the set the row already shows (a back-charted or
  // backfilled reading): keep the newer one, by the chart's own rule.
  if (observedBefore(tick, { timestamp: p.news2_at })) return p
  return {
    ...p,
    news2: risk,
    news2_at: tick.timestamp ?? p.news2_at,
    risk_level: risk.level ?? p.risk_level,
    risk_score: risk.score ?? p.risk_score,
    risk_drivers: risk.drivers ?? p.risk_drivers,
  }
}

const BAND_RANK: Record<string, number> = { critical: 2, elevated: 1, stable: 0 }

/* The ward board's order, twin of vigil_service.board_order. The server sorts
   with the same key before it pages, so page one is the top of this sort and
   no row sits above a higher NEWS2 it does not outrank: critical first (the CfC
   overlay can make a lower aggregate critical), then NEWS2 high to low, then at
   an equal NEWS2 'elevated' (a single parameter scoring 3) above 'stable',
   then nothing recorded, then id. */
function boardOrder(a: Patient, b: Patient): number {
  const key = (p: Patient): number[] => (p.news2 == null
    ? [1, 0, 0, 0]
    : [0, p.news2.level === 'critical' ? 0 : 1, -p.news2.raw_news2, -(BAND_RANK[p.news2.level] ?? 0)])
  const ka = key(a)
  const kb = key(b)
  for (let i = 0; i < ka.length; i++) if (ka[i] !== kb[i]) return ka[i] - kb[i]
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0
}

// onSelect is a STABLE callback (useCallback in the parent) and the row passes
// its own id back — so memo() actually bails out on unchanged rows. Previously
// the parent passed a fresh `() => setSelected(p.id)` closure per row per
// render, defeating memo and re-rendering all ~100 rows every 2s vitals tick.
const PatientRow = memo(({ patient: p, onSelect, newSinceReview = 0 }: {
  patient: Patient,
  onSelect: (id: string) => void,
  /* Events on this chart since THIS clinician last marked it reviewed. 0 covers
     both "nothing new" and "never reviewed", and both correctly draw nothing:
     a badge on every bed on a first login is a badge nobody reads by day two. */
  newSinceReview?: number,
}) => {
  const isEmergencySurgery = isEmergency(p);

  return (
  <button
    className={`patient-row glass-card ${isEmergencySurgery ? 'patient-row--emergency' : ''}`}
    onClick={() => onSelect(p.id)}
    style={{ width: '100%', textAlign: 'left', cursor: 'pointer' }}
  >
    <div className={`patient-row__risk patient-row__risk--${p.risk_level}`} />
    <div className="patient-row__info">
      <span className="patient-row__name">
        {p.name}
        {isEmergencySurgery && <span className="emergency-badge">EMERGENCY</span>}
        {newSinceReview > 0 && (
          <span
            className="patient-row__new-badge"
            title={`${newSinceReview} chart event${newSinceReview === 1 ? '' : 's'} since you last marked this chart reviewed`}
          >
            {newSinceReview} NEW
          </span>
        )}
      </span>
      <span className="patient-row__bed">{p.bed}</span>
    </div>
    <div className="patient-row__meta">
      <span className="patient-row__diagnosis">{p.diagnosis}</span>
    </div>
    <div className="patient-row__score">
      {/* The NEWS2 aggregate and its band, as on the nurse board and the doctor
          home. It used to print `risk_score * 100` with a "%": a 0-1 index of
          the aggregate, not a probability, and "0%" for a patient with nothing
          recorded, because (null * 100).toFixed(0) is "0". */}
      <News2Badge news2={p.news2 ?? null} labelled={false} />
    </div>
  </button>
  );
})


/* eMAR chip time: SQLite CURRENT_TIMESTAMP is UTC without a timezone marker
   ("YYYY-MM-DD HH:MM:SS"); parse it as UTC so the "Given HH:MM" chip shows the
   clinician's local wall-clock time, not UTC misread as local. */
function formatAdministeredTime(ts: string): string {
  const iso = /(?:Z|[+-]\d{2}:?\d{2})$/.test(ts) || ts.includes('T') ? ts : ts.replace(' ', 'T') + 'Z'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return ts
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}


/* ─── The 6-hour vitals trend, rendered honestly ───
   `get_vitals_trend` returns FOUR values, not three. `insufficient_data` — no
   earlier window to compare the last six hours against — is a statement about
   the RECORD, and printing it as "Stable" is a statement about the PATIENT.
   Written as lookups so a future value added on the server falls through to the
   honest default instead of quietly joining "stable". */
const TREND_LABEL: Record<string, string> = {
  deteriorating: '↘ Deteriorating',
  improving: '↗ Improving',
  stable: '→ Stable',
}
const TREND_CLASS: Record<string, string> = {
  deteriorating: 'deteriorating',
  improving: 'improving',
  stable: 'stable',
}

/* ─── The chart's ONE observation: a vital set and the NEWS2 that scored it ───
   The command bar used to hold the latest vital set in one state, replaced on
   every socket tick, and read the score from `data.risk`, which only moved when
   a tick also cleared the trend array's 5 s throttle. So for up to five
   seconds after a tick the bar showed SBP 88 beside "NEWS2 0 · STABLE" and a
   "Why NEWS2 0" breakdown under it: two observations on one screen, each
   looking current. The pair is now held together and only ever replaced as a
   whole, and every reader of "the patient now" reads it.
   The chart payload is untyped end to end (`liveData` and every row in it are
   `any`); the pair holds those same objects, so it says so once, here. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- the chart payload's own type, see above
type ChartRow = any
type LiveObservation = { patientId: string; vital: ChartRow; risk: ChartRow }
// Which set is newer: observedAt / observedBefore in lib/vitals, shared with
// the ward board so the two cannot disagree about which set is current.

/* ─── NEWS2 on the command bar ───
   Each cell is coloured by the breakdown row that scored its number and by
   nothing else. The cutoffs this replaced (HR > 100 or < 50, SBP > 160 or
   < 90, SpO2 < 94 ...) disagreed with the breakdown printed under them: SBP 90
   scores 3 and was left plain, SBP 170 scores 0 and was painted amber, and an
   unmeasured heart rate was painted amber because null < 50 is true. No row
   means nobody measured it, so it means no cue: there is no fallback. */
type BarScore = { row: News2BreakdownRow | null; points: 0 | 1 | 2 | 3 | null }

function barScore(risk: { breakdown?: unknown } | null | undefined, vital: News2ScoredVital): BarScore {
  const row = breakdownRowFor(risk?.breakdown, vital)
  if (!row) return { row: null, points: null }
  // The RCP table tops out at 3 per parameter; clamp so a stray value cannot
  // invent a fourth level. Same steps as News2Drivers' pointsClass.
  const p = row.points
  return { row, points: p >= 3 ? 3 : p >= 2 ? 2 : p >= 1 ? 1 : 0 }
}

/** The scored number's status TEXT token: danger for 3, warn for 1 and 2. */
function scoredClass(s: BarScore): string {
  return s.points === 3 ? ' command-bar__vital-scored--danger'
    : s.points === 1 || s.points === 2 ? ' command-bar__vital-scored--warn'
      : ''
}

/** The cell's aria-label clause. Zero is said out loud (measured and in range
 *  is a finding); no row says nothing. */
function scoreClause(s: BarScore, subject = 'scores'): string {
  if (!s.row || s.points === null) return ''
  return `, ${subject} ${s.points} NEWS2 point${s.points === 1 ? '' : 's'}`
    + (s.row.parameter === 'SpO2 (Scale 2)' ? ', scored on Scale 2' : '')
}

/** The cell's label with its "+N" chip beside it. The chip is the visible cue
 *  that does not depend on colour; its fills copy News2Drivers.css. */
function BarCellHead({ label, score }: { label: string; score: BarScore }) {
  return (
    <span className="command-bar__vital-head">
      <span className="command-bar__vital-label">{label}</span>
      {score.points ? (
        <span className={`command-bar__vital-points command-bar__vital-points--${score.points}`}>+{score.points}</span>
      ) : null}
    </span>
  )
}

/* ═══════════════════════════════════════════════════
   PATIENT DETAIL VIEW — vitals, notes, discharge
   ═══════════════════════════════════════════════════ */

/* `role` is threaded down rather than read from storage inside the children:
   the panel already receives the authoritative role from the shell, and
   src/lib/roles.ts treats an explicit null as "holds nothing" — a distinction
   a storage read cannot make. */
function PatientDetailView({ patientId, role, onBack }: { patientId: string; role?: string | null; onBack: () => void }) {
  const [liveData, setData] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  // Why the chart is empty. A stale patient id (a reseed regenerates every
  // PAT-… id, and deep links can inject one at any time) must not leave the
  // pillar on a dead-end screen with no way back to the ward list.
  const [loadError, setLoadError] = useState<{ status?: number; message: string } | null>(null)
  /* The newest observation, as ONE {vital, risk} pair (see LiveObservation).
     Deliberately NOT inside `data`: the offline snapshot mirrors `data` to the
     device on every change, and a per-tick write of the whole chart is the
     wrong price for a number in the bar. `data.vitals[0]` / `data.risk` are
     still kept a matching pair for the snapshot (see onVitals). */
  const [live, setLive] = useState<LiveObservation | null>(null)

  /* Both chart loaders (this one, and loadAdministrations beside the eMAR
     state it writes) are called more than once per mounted chart (mount,
     Retry, after a note is added, after a dose is charted). Two overlapping
     calls can resolve out of order, and the LOSER used to win because it wrote
     last — the clearest damage being a nurse charting a dose and watching the
     "Given" chip fail to appear because an older eMAR read landed after the
     refresh. Sequence-stamp each call and let only the newest write. */
  const detailSeqRef = useRef(0)
  const adminSeqRef = useRef(0)

  // Declared above the offline snapshot, whose reconnect handler calls it.
  const loadPatientData = async () => {
    const seq = ++detailSeqRef.current
    setLoading(true)
    try {
      const detail = await getPatientDetail(patientId)
      if (seq !== detailSeqRef.current) return
      setData(detail)
      // The response is the authority on what is stored, EXCEPT against a tick
      // that is strictly newer than its newest set: the read was answered
      // before that tick was written, and taking it would step the bar and the
      // badge back to an observation already superseded. Equal stamps: the
      // response wins. A response with NO set loses to any set on screen:
      // nothing on the chart path deletes a patient's last set, so an empty
      // answer only means the read ran before the first set was written, and
      // taking it put a new patient's first NEWS2 back to "No vitals recorded".
      const fresh: LiveObservation = { patientId, vital: detail.vitals?.[0] ?? null, risk: detail.risk }
      setLive(prev => (
        prev && prev.patientId === patientId && prev.vital
          && (!fresh.vital || observedBefore(fresh.vital, prev.vital))
          ? prev
          : fresh
      ))
      setLoadError(null)
    } catch (err) {
      console.error('Failed to load patient:', err)
      if (seq !== detailSeqRef.current) return
      const e = err as { status?: number; message?: string } | null
      setLoadError({ status: e?.status, message: e?.message || 'The patient chart could not be loaded.' })
    } finally {
      if (seq === detailSeqRef.current) setLoading(false)
    }
  }

  // Offline copy of one patient's chart — vitals, notes, medications, labs.
  // `data` is the SNAPSHOT everything below reads, so a chart served from the
  // device can never render without the banner that dates it.
  const chartOffline = useOfflineSnapshot<any>(`vigil:patient:${patientId}`, liveData, {
    onReconnect: () => loadPatientData(),
  })
  const data = chartOffline.data
  const { setActivePatient } = useActivePatient()
  // Theme-aware chart colours (axes/grid/tooltip) — see useChartTheme. Fixes the
  // hardcoded-white axes that were invisible on the light + night surfaces.
  const chartTheme = useChartTheme()
  const { setViewSelection } = useActiveView()

  useEffect(() => {
    const name = data?.patient?.name || null
    setActivePatient(patientId, name)
    // Publish the open chart as the active view selection so Sage scopes to it
    // on any tab without the clinician naming the patient.
    setViewSelection({
      kind: 'patient_chart',
      id: patientId,
      label: data?.patient?.bed ? `${name || patientId} (bed ${data.patient.bed})` : (name || patientId),
      patientId,
    })
    return () => {
      setActivePatient(null, null)
      setViewSelection(null)
    }
  }, [patientId, data?.patient?.name, data?.patient?.bed, setActivePatient, setViewSelection])

  const [dischargeModal, setDischargeModal] = useState(false)
  const [discharging, setDischarging] = useState(false)
  const [dischargeSummary, setDischargeSummary] = useState<any>(null)
  const [dischargeQrSrc, setDischargeQrSrc] = useState<string | null>(null)
  // Shown inside the modal. alert() blocked the screen and lost the reason.
  const [dischargeError, setDischargeError] = useState<string | null>(null)
  const [reviewing, setReviewing] = useState(false)
  /* The billing desk's discharge advisory, shown before the doctor confirms.
     Advisory only: it never blocks the discharge. 'unavailable' when billing
     is off at this site or the read failed, and then nothing is shown. */
  const [dischargeAdvisory, setDischargeAdvisory] = useState<DischargeAdvisory | 'loading' | 'unavailable' | null>(null)
  /* Deterioration escalations still open on this patient. Discharge used to
     leave them ticking up the tier ladder for a patient no longer on the
     ward; they are listed here to be closed with a reason. Not a block. */
  const [openEscalations, setOpenEscalations] = useState<EscalationChain[]>([])
  const [escalationReasons, setEscalationReasons] = useState<Record<string, string>>({})
  const [closingEscalation, setClosingEscalation] = useState<string | null>(null)
  const [escalationError, setEscalationError] = useState<string | null>(null)
  const [selectedNote, setSelectedNote] = useState<any>(null)
  const [selectedReport, setSelectedReport] = useState<any>(null)

  const [addNoteModalOpen, setAddNoteModalOpen] = useState(false)
  const [addMedModalOpen, setAddMedModalOpen] = useState(false)
  const [newNote, setNewNote] = useState('')
  const [newMed, setNewMed] = useState({ drug_name: '', dosage: '', route: 'Oral', frequency: '', status: 'active' })
  const [isSubmitting, setIsSubmitting] = useState(false)
  // Drug-safety gate UI: blocking = 428 needs an acknowledged override before
  // the order proceeds; info = order succeeded but raised sub-severe alerts.
  const [medSafety, setMedSafety] = useState<{ safety: MedicationSafety; mode: 'blocking' | 'info' } | null>(null)
  const [medError, setMedError] = useState<string | null>(null)
  // eMAR: per-patient administration history + the med being administered.
  const [administrations, setAdministrations] = useState<AdministrationRecord[]>([])
  const [administerTarget, setAdministerTarget] = useState<{ id: number; drug_name: string; dosage: string; route: string; frequency?: string } | null>(null)
  /* THE ALLERGY LIST THE ADMINISTER MODAL REPEATS.
     `null` is "not read" and stays null on any failure — an unread allergy list
     must never render as an empty one, so AdministerModal shows its
     "list unavailable" state rather than "NKDA". The nurse med round already had
     this list on its card; the doctor's chart had no allergy data on the client
     at all, so this is where it comes from on this path. */
  const [chartedAllergies, setChartedAllergies] = useState<AdministerAllergy[] | null>(null)
  // Live presence: other clinicians currently viewing THIS patient.
  const [viewers, setViewers] = useState<{ user_id: string; name: string; role: string }[]>([])
  const currentUser = getUser()
  /* Gated on the permission each POST needs, so a nurse or an administrator
     is not offered a button the server will refuse (src/lib/roles.ts). */
  const canDischarge = hasPermission(DISCHARGE_GENERATE, role)
  const canReleaseSummary = hasPermission(DISCHARGE_PUBLISH, role)
  const canWriteNote = hasPermission(PATIENT_WRITE, role)
  const openAddNote = () => { setNoteError(null); setAddNoteModalOpen(true) }

  /* Whether Sage's block below is showing the Tier 1 breakdown for this
     patient's alert. One breakdown on screen, never two — see the risk panel. */
  const [tier1OnScreen, setTier1OnScreen] = useState(false)

  const [notesHistoryModalOpen, setNotesHistoryModalOpen] = useState(false)
  /* One state per modal: a shared search box once left one modal opening
     pre-filtered by a term typed into another. The panel does the same for
     diagSearchQuery / labSearchQuery. */
  const [notesSearchQuery, setNotesSearchQuery] = useState('')
  const [noteError, setNoteError] = useState<string | null>(null)
  const [forecast, setForecast] = useState<any>(null)
  const [projection, setProjection] = useState<any>(null)
  const [projectionFailed, setProjectionFailed] = useState(false)

  /* 'record' is PRINT RECORD: the inpatient record, never the discharge
     summary (that one printed a "DISCHARGE SUMMARY" dated today for a patient
     still admitted). 'discharge' / 'discharge-timeline' are reachable only from
     the discharge modal, after a discharge. */
  const [printType, setPrintType] = useState<'record' | 'discharge' | 'discharge-timeline' | 'meds' | null>(null)

  // Fire print only after the print view has committed to the DOM (double rAF
  // ensures the portal content is painted), then clear printType afterwards.
  // 'meds' is excluded here: MedListPrintView owns its own print lifecycle and
  // resets the state via its onAfterPrint callback (avoids a double print dialog).
  useEffect(() => {
    if (!printType || printType === 'meds') return
    let cancelled = false
    const rafId = requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (cancelled) return
        window.print()
        setPrintType(null)
      })
    })
    return () => { cancelled = true; cancelAnimationFrame(rafId) }
  }, [printType])

  /* Discharge modal a11y: focus the primary action on open. Escape, Tab
     containment and focus restore now come from ModalOverlay's focus trap,
     which every modal in this panel shares — the bespoke window-level keydown
     listener that used to live here applied to this one modal only. */
  const dischargePrimaryBtnRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (dischargeModal) dischargePrimaryBtnRef.current?.focus()
  }, [dischargeModal])

  // Diag Reports & Lab Reports modals
  const [diagReportsModalOpen, setDiagReportsModalOpen] = useState(false)
  const [labReportsModalOpen, setLabReportsModalOpen] = useState(false)
  const [medsListModalOpen, setMedsListModalOpen] = useState(false)
  const [storyOpen, setStoryOpen] = useState(false)
  const [selectedLabForGraph, setSelectedLabForGraph] = useState<any>(null)
  
  const [diagSearchQuery, setDiagSearchQuery] = useState('')
  const [labSearchQuery, setLabSearchQuery] = useState('')
  const [reportOpenedFrom, setReportOpenedFrom] = useState<'diag' | 'lab' | null>(null)

  /* Sage-deduced clinical course (cached server-side; safe to fetch on each open).
     The rejection used to be swallowed, which left `projection` null — and null
     is the component's LOADING state, so a failed projection pulsed "Sage is
     charting the expected course…" indefinitely. Track the failure explicitly
     so the timeline can say so and offer a retry. */
  const loadProjection = useCallback(() => {
    setProjection(null)
    setProjectionFailed(false)
    getTimelineProjection(patientId)
      .then(p => { setProjection(p); setProjectionFailed(false) })
      .catch(() => setProjectionFailed(true))
  }, [patientId])

  // eMAR history drives the per-med "Given HH:MM" chips; refreshed on load and
  // after each recorded administration.
  const loadAdministrations = async () => {
    const seq = ++adminSeqRef.current
    try {
      const records = await getAdministrations(patientId)
      if (seq !== adminSeqRef.current) return
      setAdministrations(records)
    } catch (err) {
      console.error('Failed to load administrations:', err)
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    loadPatientData()
    loadAdministrations()
    // Fetch forecast for this patient via the centralized client (auth + 401 handling).
    getPatientForecast(patientId).then(setForecast).catch(() => {})
    loadProjection()
  }, [patientId, loadProjection])

  /* Charted allergies for the Administer modal. Cleared to `null` on every
     patient change BEFORE the fetch, so a slow response can never leave the
     previous patient's allergy list on this chart — the failure mode would be
     an allergy banner naming a substance that belongs to somebody else. */
  useEffect(() => {
    let live = true
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    setChartedAllergies(null)
    apiFetch<{ allergies?: AdministerAllergy[] } | AdministerAllergy[]>(`/patients/${patientId}/allergies`)
      .then(res => {
        if (!live) return
        const list = Array.isArray(res) ? res : res?.allergies
        // Only an array is a list. Anything else stays "not read".
        if (Array.isArray(list)) setChartedAllergies(list)
      })
      .catch(err => {
        console.error('Failed to load allergies:', err)
        if (live) setChartedAllergies(null)
      })
    return () => { live = false }
  }, [patientId])

  // Live presence: announce that we're viewing this patient and render who else
  // is. enter/leave are re-emitted on reconnect (fresh sid loses room membership).
  useEffect(() => {
    const enter = () => socket.emit('enter_patient', { patient_id: patientId })
    const handlePresence = (data: { patient_id: string; viewers: { user_id: string; name: string; role: string }[] }) => {
      if (data.patient_id === patientId) setViewers(data.viewers || [])
    }
    enter()
    socket.on('presence:update', handlePresence)
    socket.on('connect', enter)
    return () => {
      socket.off('presence:update', handlePresence)
      socket.off('connect', enter)
      socket.emit('leave_patient', { patient_id: patientId })
      setViewers([])
    }
  }, [patientId])

  useEffect(() => {
    const onVitals = (newVital: any) => {
      if (newVital.patient_id === patientId) {
        // A set with no score cannot be shown: the bar would have to pair it
        // with an older observation's NEWS2. Both server emitters always send
        // one, so a tick without it is malformed and is dropped whole, trend
        // array included.
        if (!newVital.risk || typeof newVital.risk !== 'object') return

        // Never step back. A tick strictly OLDER than the pair on screen (a
        // backdated manual set, a late delivery) is history, not "now". The
        // trend throttle below drops it too: its gap is negative.
        setLive(prev => (
          prev && prev.patientId === patientId && observedBefore(newVital, prev.vital)
            ? prev
            : { patientId, vital: newVital, risk: newVital.risk }
        ))

        // Add to Trend Chart if it's been at least 5-10 seconds for smoothness
        setData((prev: any) => {
          if (!prev) return prev;
          // observedAt, not new Date(): a reloaded row can carry a bare SQLite UTC
          // stamp, which new Date() reads as LOCAL time. In IST that dated the
          // stored set 5.5 h early, so a tick hours OLDER than it cleared the
          // throttle and became the trend's newest point, and the offline
          // snapshot then showed it with its score. A set either side cannot
          // date is appended, as before.
          const lastTs = observedAt(prev.vitals[0])
          const nowTs = observedAt(newVital)

          if (lastTs !== null && nowTs !== null && nowTs - lastTs < 5000) return prev; // 5s throttle for better graph resolution
          
          // The appended set and its own score, together, so the offline
          // snapshot's vitals[0] and risk stay one observation.
          return {
            ...prev,
            vitals: [newVital, ...prev.vitals].slice(0, 2000), // Larger history
            risk: newVital.risk
          };
        });
      }
    };
    socket.on('vitals:update', onVitals);
    return () => { socket.off('vitals:update', onVitals); }
  }, [patientId])

  const handleDischarge = async () => {
    setDischarging(true)
    setDischargeError(null)
    try {
      const result = await dischargePatient(patientId)
      if (result?.status === 'already_discharged') {
        setDischargeError('This patient has already been discharged.')
      } else {
        setDischargeSummary(result)
      }
    } catch (err: any) {
      setDischargeError(`The discharge did not complete: ${err?.message || 'the server did not say why'}.`)
    } finally {
      setDischarging(false)
    }
  }

  /* The doctor has read the drafted summary: release it to the patient's
     portal and discharge link. Names the summary she read, so a newer draft
     can never be released by this click. */
  const handleReviewSummary = async () => {
    if (!dischargeSummary?.summary_id) return
    setReviewing(true)
    setDischargeError(null)
    try {
      assertOnlineForWrite('release this summary')
      const view = await apiFetch<any>(`/vigil/patients/${patientId}/discharge-summary/review`, {
        method: 'POST',
        body: JSON.stringify({ summary_id: dischargeSummary.summary_id }),
      })
      setDischargeSummary((prev: any) => ({
        ...prev,
        review_status: view?.review_status ?? 'reviewed',
        reviewed_by_name: view?.reviewed_by_name ?? null,
        reviewed_at: view?.reviewed_at ?? null,
      }))
    } catch (err: any) {
      setDischargeError(`The summary was not released: ${err?.message || 'the server did not say why'}.`)
    } finally {
      setReviewing(false)
    }
  }

  // Open escalation chains, read when the discharge modal opens.
  useEffect(() => {
    if (!dischargeModal) return
    let live = true
    // Inside the promise chain so any failure, even a synchronous one, lands
    // in the catch: this list must never stop the discharge screen opening.
    Promise.resolve()
      .then(() => getOpenEscalations(patientId))
      .then(rows => { if (live) setOpenEscalations(Array.isArray(rows) ? rows : []) })
      .catch(() => { if (live) setOpenEscalations([]) })
    return () => { live = false }
  }, [dischargeModal, patientId])

  const closeEscalation = async (id: string) => {
    const reason = (escalationReasons[id] || '').trim()
    if (reason.length < 3) return
    setClosingEscalation(id)
    setEscalationError(null)
    try {
      await resolveEscalation(id, reason)
      setOpenEscalations(prev => prev.filter(e => e.id !== id))
      setEscalationReasons(prev => { const next = { ...prev }; delete next[id]; return next })
    } catch (err) {
      setEscalationError(`The escalation was not closed: ${(err instanceof Error && err.message) || 'the server did not say why'}.`)
    } finally {
      setClosingEscalation(null)
    }
  }

  // The billing advisory, read when the discharge modal opens.
  useEffect(() => {
    if (!dischargeModal) return
    let live = true
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger: the loader's own loading flag.
    setDischargeAdvisory('loading')
    getDischargeAdvisory(patientId)
      .then(a => { if (live) setDischargeAdvisory(a && typeof a === 'object' && 'headline' in a ? a : 'unavailable') })
      .catch(() => { if (live) setDischargeAdvisory('unavailable') })
    return () => { live = false }
  }, [dischargeModal, patientId])

  // Discharge QR requires auth → fetch it as a blob and use an object URL.
  useEffect(() => {
    const qrUrl = dischargeSummary?.qr_code_url
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    if (!qrUrl) { setDischargeQrSrc(null); return }
    let revoked = false
    let objUrl: string | null = null
    getDischargeQrObjectUrl(qrUrl)
      .then((url) => {
        if (revoked) { URL.revokeObjectURL(url); return }
        objUrl = url
        setDischargeQrSrc(url)
      })
      .catch((e) => console.error('Failed to load discharge QR:', e))
    return () => {
      revoked = true
      if (objUrl) URL.revokeObjectURL(objUrl)
      setDischargeQrSrc(null)
    }
  }, [dischargeSummary?.qr_code_url])

  const handleAddNote = async () => {
    if (!newNote.trim()) return
    setIsSubmitting(true)
    try {
      // addClinicalNote routes through offlineMutate, which queues with no
      // connection and hands back an optimistic note the chart then renders. A
      // queued note can still fail permanently on replay and be dropped, and by
      // then the author has read it back on screen and moved on. Refuse instead:
      // the typed text stays in the box for her to send when she reconnects.
      assertOnlineForWrite('save this note')
      await addClinicalNote(patientId, newNote)
      setAddNoteModalOpen(false)
      setNewNote('')
      setNoteError(null)
      loadPatientData()
    } catch (e) {
      // The refusal explains itself far better than "Failed to add note" does,
      // and it stays in the modal beside the text rather than in an alert().
      setNoteError(e instanceof Error ? e.message : 'The note was not saved.')
    } finally {
      setIsSubmitting(false)
    }
  }

  const submitMedication = async (safetyAck?: SafetyAck) => {
    setIsSubmitting(true)
    setMedError(null)
    try {
      const result = await addMedication(patientId, newMed, safetyAck)
      setMedSafety(null)
      setAddMedModalOpen(false)
      setNewMed({ drug_name: '', dosage: '', route: 'Oral', frequency: '', status: 'active' })
      // Sub-severe alerts (or an unscreened drug) still deserve eyes: surface
      // them once, informationally, after the order succeeds. Skipped on an
      // acknowledged re-submit — the clinician just reviewed those alerts.
      const safety: MedicationSafety | undefined = result?.safety
      if (!safetyAck && safety && (safety.max_severity !== 'none' || safety.interaction_screened === false)) {
        setMedSafety({ safety, mode: 'info' })
      }
      loadPatientData()
    } catch (e) {
      if (isSafetyAckRequired(e)) {
        // 428: severe alerts — open the blocking acknowledgement modal.
        setMedSafety({ safety: e.safety, mode: 'blocking' })
      } else {
        setMedError(e instanceof Error ? e.message : 'Failed to add medication')
      }
    } finally {
      setIsSubmitting(false)
    }
  }

  const handleAddMed = () => {
    if (!newMed.drug_name || !newMed.dosage || !newMed.frequency) {
      setMedError('Please fill out drug name, dosage, and frequency.')
      return
    }
    submitMedication()
  }

  const vitals = data?.vitals || []


  const filteredHistoryNotes = useMemo(() => {
    const all = [...(data?.notes||[]), ...(data?.scribe_notes||[])].sort((a,b) => parseUTC(b.created_at).getTime() - parseUTC(a.created_at).getTime())
    if (!notesSearchQuery) return all
    const q = notesSearchQuery.trim().toLowerCase()
    // A typed date is matched as a calendar day ("1 Oct 2026", "01/10/2026"),
    // not as a substring of the rendered date, which had asked for US order.
    const dateMatch = parseDateSearch(notesSearchQuery)
    return all.filter(n =>
      (dateMatch && matchesDateSearch(n.created_at, dateMatch)) ||
      (n.content || n.structured_note || n.raw_transcript || '').toLowerCase().includes(q) ||
      formatUTCDate(n.created_at).toLowerCase().includes(q) ||
      (n.doc_type || n.note_format || '').toLowerCase().includes(q)
    )
  }, [data?.notes, data?.scribe_notes, notesSearchQuery])

  const IMAGING_TYPES = ['xray', 'x-ray', 'ct', 'mri', 'ultrasound', 'ecg', 'echo']

  // All diagnostic reports (imaging only)
  const allDiagnosticReports = useMemo(() => {
    return (data?.lens_analyses || []).filter((a: any) => 
      IMAGING_TYPES.some(t => (a.image_type || '').toLowerCase().includes(t))
    ).sort((a: any, b: any) => parseUTC(b.created_at).getTime() - parseUTC(a.created_at).getTime())
  }, [data?.lens_analyses])

  // All lab reports (non-imaging lens_analyses + clinical_documents with lab_result type)
  const allLabReports = useMemo(() => {
    const labLens = (data?.lens_analyses || []).filter((a: any) => 
      !IMAGING_TYPES.some(t => (a.image_type || '').toLowerCase().includes(t))
    )
    // Also gather lab_result docs from notes
    const labDocs = (data?.notes || []).filter((n: any) => n.doc_type === 'lab_result').map((n: any) => ({
      ...n,
      image_type: 'lab',
      analysis: n.content,
      clinical_context: n.title,
      image_path: '',
    }))
    // Merge, deduplicate by content similarity, sort newest first
    const combined = [...labLens, ...labDocs]
    const seen = new Set<string>()
    return combined.filter((r: any) => {
      const key = `${r.clinical_context || r.title || ''}_${(r.created_at || '').substring(0, 16)}`
      if (seen.has(key)) return false
      seen.add(key)
      return true
    }).sort((a: any, b: any) => parseUTC(b.created_at).getTime() - parseUTC(a.created_at).getTime())
  }, [data?.lens_analyses, data?.notes])

  // Filtered diag reports
  const filteredDiagReports = useMemo(() => {
    if (!diagSearchQuery) return allDiagnosticReports
    const q = diagSearchQuery.trim().toLowerCase()
    const dateMatch = parseDateSearch(diagSearchQuery)
    return allDiagnosticReports.filter((a: any) => {
      if (dateMatch && matchesDateSearch(a.created_at, dateMatch)) return true
      return (
        (a.analysis || '').toLowerCase().includes(q) ||
        (a.image_type || '').toLowerCase().includes(q) ||
        (a.clinical_context || '').toLowerCase().includes(q) ||
        formatUTCDate(a.created_at).toLowerCase().includes(q)
      )
    })
  }, [allDiagnosticReports, diagSearchQuery])

  // Filtered lab reports
  const filteredLabReports = useMemo(() => {
    if (!labSearchQuery) return allLabReports
    const q = labSearchQuery.trim().toLowerCase()
    const dateMatch = parseDateSearch(labSearchQuery)
    return allLabReports.filter((a: any) => {
      if (dateMatch && matchesDateSearch(a.created_at, dateMatch)) return true
      return (
        (a.analysis || a.content || '').toLowerCase().includes(q) ||
        (a.clinical_context || a.title || '').toLowerCase().includes(q) ||
        (a.image_type || '').toLowerCase().includes(q) ||
        formatUTCDate(a.created_at).toLowerCase().includes(q)
      )
    })
  }, [allLabReports, labSearchQuery])


  const pData = data?.patient || {}

  // Comorbidities come only from real recorded data. The previous build
  // fabricated 2-4 conditions by hashing the MRN and rendered them unlabelled —
  // a patient-safety hazard (a clinician could act on invented history). We now
  // show only genuine problem-list entries the patient record carries; if none
  // are recorded, the section renders nothing rather than inventing them.
  const patientC: string[] = Array.isArray(pData.comorbidities)
    ? pData.comorbidities.filter((c: unknown): c is string => typeof c === 'string' && c.trim().length > 0)
    : []

  // Recorded results only, each at the time it was observed (see buildLabTrends).
  const labTrendData = useMemo(
    () => buildLabTrends(data?.notes, data?.lab_observations),
    [data?.notes, data?.lab_observations],
  )

  if (loading) return <div className="orb-panel animate-fade-in"><div className="orb-panel-loading">Loading patient data…</div></div>
  if (!data) {
    // Always render the back control here: without it a stale id (deep link, or
    // a chart left open across a reseed) strands the whole Vigil pillar until a
    // full browser reload — the selection survives tab switches.
    const notFound = loadError?.status === 404
    return (
      <div className="orb-panel animate-fade-in detail">
        <div className="command-bar">
          <button className="command-bar__back" onClick={onBack} aria-label="Back to ward list">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
          </button>
          <div className="command-bar__identity">
            <h2 className="command-bar__name">{notFound ? 'Patient not found' : 'Chart unavailable'}</h2>
            <div className="command-bar__mrn">{patientId}</div>
          </div>
        </div>
        <div className="orb-panel-loading" role="alert">
          <div>
            {notFound
              ? 'This record may have been discharged, merged or removed since the link was created.'
              : loadError?.message || 'The patient chart could not be loaded.'}
          </div>
          <div style={{ marginTop: 12, display: 'flex', gap: 8, justifyContent: 'center' }}>
            <button
              onClick={onBack}
              style={{ padding: '6px 14px', borderRadius: 6, border: '1px solid var(--border-glass)', background: 'var(--btn-gradient)', color: 'var(--btn-primary-text)', fontWeight: 600, cursor: 'pointer' }}
            >
              Back to ward list
            </button>
            {!notFound && (
              <button
                onClick={loadPatientData}
                style={{ padding: '6px 14px', borderRadius: 6, border: '1px solid var(--border-glass)', background: 'transparent', color: 'var(--text-secondary)', fontWeight: 600, cursor: 'pointer' }}
              >
                Retry
              </button>
            )}
          </div>
        </div>
      </div>
    )
  }

  // `risk` is deliberately NOT read from `data`: there it is the throttled
  // trend array's score. Everything below reads the one observation.
  const { patient, trend, notes, medications } = data
  /* ONE observation for every reader of "the patient now": badge, bar values,
     bar colours, gauge, drivers, breakdown, Sage's risk level, the discharge
     modal and the handoff print. Live chart: the pair (the panel is keyed by
     patient, and the id check makes a stale pair unreadable regardless). A
     chart served from the device: the snapshot's own newest set and its
     score, which onVitals keeps paired. Only the sparkline and the throttle
     read the trend array. */
  const obs: Omit<LiveObservation, 'patientId'> = chartOffline.source === 'live' && live && live.patientId === patientId
    ? live
    : { vital: vitals[0] ?? null, risk: data.risk }
  const latest = obs.vital ?? {}
  const risk = obs.risk
  // Honesty label: the row on the bar came from the simulator. A simulator
  // push over the socket names no source, so it falls back to the chart's flag.
  const vitalsSimulated = latest.source ? latest.source === 'simulated' : !!data.simulated_vitals
  const hrScore = barScore(risk, 'heart_rate')
  const sbpScore = barScore(risk, 'systolic_bp')
  const spo2Score = barScore(risk, 'spo2')
  const tempScore = barScore(risk, 'temperature')
  const rrScore = barScore(risk, 'respiratory_rate')

  // ─── Render Sparkline ───
  // The window is chosen by time and the label states the span it really
  // covers (see src/lib/vitalTrend.ts). It used to be the newest 40 entries
  // under a fixed "6H" label: about 200 s on the 5-second simulator lane, about
  // a week on a 4-hourly ward. Computed once per vital per render, because the
  // cell's aria-label and its tooltip must describe the same window.
  const trendWindows: Record<string, VitalTrendWindow | null> = {}
  const trendWindowFor = (dataKey: string) => (
    dataKey in trendWindows ? trendWindows[dataKey] : (trendWindows[dataKey] = vitalTrendWindow(vitals, dataKey))
  )
  /** The aria-label's trend clause, or nothing when there is no trend to open. */
  const trendAria = (dataKey: string, what?: string) => {
    const win = trendWindowFor(dataKey)
    return win ? `: ${trendAriaPhrase(win, what)}` : ''
  }

  const renderSparkline = (label: string, dataKey: string) => {
    const win = trendWindowFor(dataKey)
    if (!win) return null
    const { min, max } = win
    const range = max - min || 1

    // Canvas dimensions and padding to prevent stroke clipping
    const w = 80, h = 24;
    const paddingX = 2;
    const paddingY = 2;
    const drawW = w - (paddingX * 2);
    const drawH = h - (paddingY * 2);

    // x is TIME, not index: four-hourly sets with one hourly recheck between
    // them must not be drawn as if they were evenly spaced.
    const points = win.points.map(p => {
      const x = paddingX + ((p.t - win.firstMs) / win.spanMs) * drawW;
      const y = paddingY + drawH - ((p.v - min) / range) * drawH;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');

    // As charted, the same number the cell and the NEWS2 drivers print.
    const fmt = (n: number) => (dataKey === 'temperature' ? vitalTemperature(n) : vitalWhole(n))
    const minStr = fmt(min);
    const maxStr = fmt(max);
    const axisEnds = [trendAxisLabel(win.firstMs), trendAxisLabel(win.lastMs)]

    return (
      <div className="command-bar__vital-tooltip" style={{ width: '140px', padding: '10px 12px' }}>
        <div className="vital-trend-header" style={{ borderBottom: '1px solid var(--border-glass)', paddingBottom: '4px', marginBottom: '6px', textAlign: 'left', width: '100%' }}>{label} TREND ({trendSpanShort(win.spanMs)})</div>
        <div style={{ display: 'flex', gap: '6px', width: '100%', alignItems: 'center' }}>
          {/* Y-axis labels */}
          <div data-trend-axis="y" style={{ display: 'flex', flexDirection: 'column', justifyContent: 'space-between', height: `${h}px`, fontSize: '0.5rem', color: 'var(--text-secondary)', fontFamily: 'monospace', textAlign: 'right', flexShrink: 0, lineHeight: 1 }}>
            <span>{maxStr}</span>
            <span>{minStr}</span>
          </div>
          {/* Chart area */}
          <div style={{ position: 'relative', width: `${w}px`, height: `${h}px`, flexShrink: 0 }}>
            <div style={{ position: 'absolute', top: 0, left: 0, right: 0, borderTop: '1px dashed var(--border-subtle)' }} />
            <div style={{ position: 'absolute', bottom: 0, left: 0, right: 0, borderBottom: '1px dashed var(--border-subtle)' }} />
            <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} style={{ display: 'block' }}>
              <polyline points={points} fill="none" stroke="var(--accent-cyan)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
        </div>
        {/* X-axis labels: when the first and last plotted observations were
            taken, never "-6h" and "now". The date line appears only off today. */}
        <div data-trend-axis="x" style={{ display: 'flex', justifyContent: 'space-between', width: '100%', marginTop: '4px', paddingLeft: '22px', fontSize: '0.5rem', color: 'var(--text-muted)', fontFamily: 'monospace' }}>
          {axisEnds.map((end, i) => (
            <span key={i} style={{ textAlign: i === 0 ? 'left' : 'right' }}>
              {end.date && <span style={{ display: 'block' }}>{end.date}</span>}
              {end.time}
            </span>
          ))}
        </div>
      </div>
    );
  };

  return (
    <div className="orb-panel animate-fade-in detail">
      {/* Above the chart, not beside it: every vital, note and drug row below is
          only as current as this line says it is. */}
      <OfflineBanner snapshot={chartOffline} what="patient chart" onRetry={loadPatientData} />
      {/* ═══ Compact Clinical Command Center (Stitch Design) ═══ */}
      <div className="command-bar">
        <button className="command-bar__back" onClick={onBack} aria-label="Back to ward list">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
        </button>

        {/* Patient Identity */}
        <div className="command-bar__identity">
          <h2 className="command-bar__name">{patient.name}</h2>
          <div className="command-bar__mrn">
            MRN: {patient.mrn} · {patient.age}Y · {sexLabel(patient.sex)}
          </div>
          <PresenceChips viewers={viewers} selfId={currentUser?.id} />
          {vitalsSimulated && (
            <span
              role="note"
              title="These vital signs are generated by the simulator. They are not readings from this patient."
              style={{ alignSelf: 'flex-start', marginTop: 4, padding: '2px 8px', borderRadius: 999, fontSize: '0.7rem', fontWeight: 600, color: 'var(--status-warn-text)', border: '1px solid var(--status-warn)', background: 'var(--bg-glass)' }}
            >
              Simulated vitals
            </span>
          )}
        </div>

        {/* Bed + Risk Badge — NEWS2 aggregate + band, not a percentage. */}
        <div className={`command-bar__bed-badge command-bar__bed-badge--${risk.level}`}>
          <span className="command-bar__bed-label">BED {patient.bed}</span>
          {/* The title carries what the visible text cannot: the Scale 2 and
              single-parameter-3 notes, in the badge's one shared sentence. */}
          <span className="command-bar__risk-pct" title={`${news2Description(risk)}. NEWS2 aggregate (RCP early-warning table), not a validated probability`}>
            {typeof risk.raw_news2 === 'number' ? `NEWS2 ${risk.raw_news2} · ` : ''}{risk.level.toUpperCase()}
          </span>
        </div>

        {/* Diagnosis & Comorbidities (comorbidity tags only when genuinely recorded) */}
        <div className="command-bar__dx">
          <span className="command-bar__dx-label">{patientC.length ? 'DIAGNOSIS & COMORBIDITIES' : 'DIAGNOSIS'}</span>
          <span className="command-bar__dx-value">{patient.diagnosis}</span>
          {patientC.length > 0 && (
            <div className="command-bar__comorbidities">
              {patientC.map(c => <span key={c} className="command-bar__comorbidity-tag">{c}</span>)}
            </div>
          )}
        </div>

        {/* Vital Sign Cells */}
        {/* Each cell is focusable (tabIndex 0) because its tooltip holds the
            trend sparkline: clinical data, not a label gloss. The trend covers
            at most the 6 hours ending at the newest observation and its
            header, axis and the aria-label's trend clause (trendAria) state
            the span it really covers; with under two timed readings there is
            no tooltip and the label offers none. Hover
            alone left that unreachable on the tablet the ward round runs on;
            Vigil.css pairs :hover with :focus/:focus-within so a tap or a Tab
            opens the same trend.
            Colour, the "+N" chip, `data-news2-points` and the label's NEWS2
            clause all come from the breakdown row of the SAME observation as
            the number (barScore, above); no row, no cue. role="group" so the
            aria-label is announced: on a role-less div it is not reliably
            exposed. */}
        <div className="command-bar__vitals">
          <div
            className="command-bar__vital"
            role="group"
            tabIndex={0}
            data-news2-points={hrScore.points ?? undefined}
            aria-label={`Heart rate ${vitalWhole(latest.heart_rate, 'not recorded')} bpm${scoreClause(hrScore)}${trendAria('heart_rate')}`}
          >
            <BarCellHead label="HR" score={hrScore} />
            <span className={`command-bar__vital-value${scoredClass(hrScore)}`}>{vitalWhole(latest.heart_rate)}</span>
            <span className="command-bar__vital-unit">bpm</span>
            {renderSparkline('HR', 'heart_rate')}
          </div>
          {/* NEWS2 scores the systolic only, so only the systolic number is
              coloured; the diastolic stays neutral whatever it reads. */}
          <div
            className="command-bar__vital"
            role="group"
            tabIndex={0}
            data-news2-points={sbpScore.points ?? undefined}
            aria-label={`Blood pressure ${vitalWhole(latest.systolic_bp, 'not recorded')} over ${vitalWhole(latest.diastolic_bp, 'not recorded')} mmHg${scoreClause(sbpScore, 'systolic scores')}${trendAria('systolic_bp', 'systolic trend')}`}
          >
            <BarCellHead label="BP" score={sbpScore} />
            <span className="command-bar__vital-value"><span className={`command-bar__vital-systolic${scoredClass(sbpScore)}`}>{vitalWhole(latest.systolic_bp)}</span>/{vitalWhole(latest.diastolic_bp)}</span>
            <span className="command-bar__vital-unit">mmHg</span>
            {renderSparkline('SYS BP', 'systolic_bp')}
          </div>
          <div
            className="command-bar__vital"
            role="group"
            tabIndex={0}
            data-news2-points={spo2Score.points ?? undefined}
            aria-label={`Oxygen saturation ${vitalWhole(latest.spo2, 'not recorded')} percent${scoreClause(spo2Score)}${trendAria('spo2')}`}
          >
            <BarCellHead label="SPO2" score={spo2Score} />
            <span className={`command-bar__vital-value${scoredClass(spo2Score)}`}>{vitalWhole(latest.spo2)}</span>
            <span className="command-bar__vital-unit">%</span>
            {renderSparkline('SPO2', 'spo2')}
          </div>
          <div
            className="command-bar__vital"
            role="group"
            tabIndex={0}
            data-news2-points={tempScore.points ?? undefined}
            aria-label={`Temperature ${vitalTemperature(latest.temperature, 'not recorded')} degrees Celsius${scoreClause(tempScore)}${trendAria('temperature')}`}
          >
            <BarCellHead label="TEMP" score={tempScore} />
            <span className={`command-bar__vital-value${scoredClass(tempScore)}`}>{vitalTemperature(latest.temperature)}</span>
            <span className="command-bar__vital-unit">°C</span>
            {renderSparkline('TEMP', 'temperature')}
          </div>
          <div
            className="command-bar__vital"
            role="group"
            tabIndex={0}
            data-news2-points={rrScore.points ?? undefined}
            aria-label={`Respiratory rate ${vitalWhole(latest.respiratory_rate, 'not recorded')} per minute${scoreClause(rrScore)}${trendAria('respiratory_rate')}`}
          >
            <BarCellHead label="RR" score={rrScore} />
            <span className={`command-bar__vital-value${scoredClass(rrScore)}`}>{vitalWhole(latest.respiratory_rate)}</span>
            <span className="command-bar__vital-unit">rpm</span>
            {renderSparkline('RR', 'respiratory_rate')}
          </div>
        </div>

        {/* Action buttons, in sentence case and one style, grouped by what
            they do: read the chart, hand it over, print it, discharge last.
            The ids are what the tests and the visual driver click. */}
        <div className="command-bar__actions">
          <div className="command-bar__group" role="group" aria-label="View">
            <button className="command-bar__btn" onClick={() => { setDiagSearchQuery(''); setDiagReportsModalOpen(true); }} id="btn-diag-reports">
              <Icon name="image" size={13} /> Diagnostics
            </button>
            <button className="command-bar__btn" onClick={() => { setLabSearchQuery(''); setLabReportsModalOpen(true); }} id="btn-lab-reports">
              <Icon name="flask" size={13} /> Labs
            </button>
            <button className="command-bar__btn" onClick={() => setMedsListModalOpen(true)} id="btn-meds-reports">
              <Icon name="pill" size={13} /> Medications
            </button>
            <button className="command-bar__btn" onClick={() => setNotesHistoryModalOpen(true)} id="btn-notes">
              <Icon name="note" size={13} /> Notes
            </button>
            {canWriteNote && (
              <button className="command-bar__btn" onClick={openAddNote} id="btn-add-note">
                Add note
              </button>
            )}
            <button className="command-bar__btn" onClick={() => setStoryOpen(true)} id="btn-story" title="One scrollable clinical narrative, every event on a single filterable timeline">
              <Icon name="timeline" size={13} /> Story
            </button>
          </div>
          <div className="command-bar__group" role="group" aria-label="Hand over">
            <button className="command-bar__btn" onClick={() => window.open(`/handoff/${patientId}`, '_blank')} title="Auto-drafted I-PASS shift handoff">
              <Icon name="handoff" size={13} /> Handoff
            </button>
            <button className="command-bar__btn" onClick={() => window.open(`/downtime/${patientId}`, '_blank')} title="Read-only offline downtime snapshot">
              <Icon name="offline" size={13} /> Downtime
            </button>
          </div>
          <div className="command-bar__group" role="group" aria-label="Print">
            <button className="command-bar__btn" onClick={() => setPrintType('record')} title="The inpatient record as charted: identity, allergies, problems, active medicines, latest vitals and signed notes">
              <Icon name="printer" size={13} /> Print record
            </button>
            <button className="command-bar__btn" onClick={() => setPrintType('meds')}>
              <Icon name="printer" size={13} /> Print medications
            </button>
          </div>
          {canDischarge && (
            <div className="command-bar__group" role="group" aria-label="Discharge">
              <button className="command-bar__btn" onClick={() => { setDischargeError(null); setDischargeModal(true) }}>
                <Icon name="exit" size={13} /> Discharge
              </button>
            </div>
          )}
        </div>
      </div>
      <CareBundleTimer patientId={patientId} />


      
      {/* Sage-deduced clinical course (replaces the calendar-day rail) */}
      <ClinicalCourseTimeline projection={projection} failed={projectionFailed} onRetry={loadProjection} />

      {/* Risk Assessment & Lab Trends */}
      <div className="detail__row">
        <div className="detail__risk-panel glass-card">
          <h3 className="detail__section-title">Orb Risk Assessment</h3>
          <div className="detail__risk-top-section" style={{ display: 'flex', gap: '16px', alignItems: 'center', marginBottom: '8px', marginTop: '8px' }}>
            <div className="detail__risk-gauge" style={{ margin: 0, flexShrink: 0 }}>
              <RiskGauge score={risk.score} level={risk.level} rawNews2={risk.raw_news2} />
            </div>
            <div className="detail__risk-drivers" style={{ flex: 1, marginTop: 0, maxHeight: 'none', overflowY: 'visible' }}>
              {risk.drivers?.map((d: string, i: number) => (
                <div key={i} className="detail__driver" style={{ marginBottom: '4px' }}>{d}</div>
              ))}
            </div>
          </div>
          {/* ── The 6-hour trend, and what it is a trend OF ──
                 `trend.trend` is a comparison of the last 6 hours of vitals
                 against the 6 before them. It has a FOURTH value —
                 `insufficient_data`, returned whenever there is no earlier
                 window to compare against — and this line used to sweep it into
                 the `else` branch and print "→ Stable". On the seeded ward that
                 meant every chart read "Trend: → Stable", including a patient
                 whose systolic BP fell 108 → 65 across six observations. "We
                 have no baseline" and "this patient is steady" are opposite
                 statements to a nurse deciding whether to escalate.
                 The basis is named too: a 6-hour average is not the last
                 observation, and the two genuinely disagree on a patient who
                 fell in the last twenty minutes. ── */}
          <div className={`detail__trend detail__trend--${TREND_CLASS[trend.trend] || 'unknown'}`}>
            Trend (last 6h vs the 6h before): {TREND_LABEL[trend.trend] || 'not enough earlier observations to compare'}
          </div>
          {/* ── Why this NEWS2 — ONE breakdown on this screen, never two ──
                 Sage's block below carries Tier 1: the same decomposition of the
                 observation THE ALERT FIRED ON, plus the movement sentences and
                 the deterministic headline the server writes and nothing used to
                 read. It is strictly richer than this one, so when it is on
                 screen this live copy stands down rather than printing a second,
                 differently-timed answer to "why NEWS2?" beside it.
                 When there is no alert (or the assessment feature is off) this
                 is the only breakdown there is, and every chart keeps one.

                 The emptiness guard is not tidiness: with no vitals on file the
                 engine returns an EMPTY breakdown, and a decomposition panel
                 rendered over it would print "every measured vital is in the
                 normal range" about a patient nobody has measured. Absent and
                 normal are not the same finding. ── */}
          {!tier1OnScreen
            && Array.isArray(risk.breakdown) && risk.breakdown.length > 0
            && typeof risk.raw_news2 === 'number' && (
            <News2Drivers
              view={chartDriverView(risk.breakdown, risk.raw_news2, risk.single_param_3)}
              id={`chart-${patientId}`}
            />
          )}
        </div>

        {/* Lab Trends */}
        <div className="detail__labs-panel glass-card">
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
            <h3 className="detail__section-title" style={{ margin: 0 }}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ verticalAlign: 'middle', marginRight: 8 }}>
                <path d="M9 3h6v7l4 9H5l4-9V3z"/><line x1="9" y1="3" x2="15" y2="3"/>
              </svg>
              Lab Trends
            </h3>
          </div>
          {labTrendData.length === 0 ? (
            <div className="detail__no-data" data-testid="labs-empty">No lab results recorded for this patient</div>
          ) : (
          <div className="detail__labs-grid">
            {labTrendData.map((lab: LabTrend, idx: number) => {
              // Fill token for the edge and the line, text token for the value
              // (the house contrast rule). A result with no range and no flag
              // is not coloured: there is nothing to judge it against.
              const statusColor = LAB_STATUS_FILL[lab.status]
              const statusText = LAB_STATUS_TEXT[lab.status]
              const trendArrow = lab.trendDir === 'up' ? '↑' : lab.trendDir === 'down' ? '↓' : '→'
              // See labTrendTone: the arrow is coloured by movement RELATIVE TO
              // THE REFERENCE RANGE, never by up/down. Falling platelets in
              // dengue are not green here.
              const trendTone = labTrendTone(lab)
              const trendMeaning = labTrendLabel(trendTone, lab.trendDir, lab)
              const pts = lab.points
              const hasRange = lab.refLow !== null && lab.refHigh !== null
              const minV = Math.min(...pts, ...(hasRange ? [lab.refLow as number, lab.refHigh as number] : []))
              const maxV = Math.max(...pts, ...(hasRange ? [lab.refLow as number, lab.refHigh as number] : []))
              const range = maxV - minV || 1
              const chartW = 180
              const chartH = 70
              const padY = 6
              // x by the time each result was observed, not by its index.
              const ts = labGraphData(lab).map(p => p.t)
              const t0 = Math.min(...ts), span = Math.max(...ts) - t0
              const xAt = (i: number) => (pts.length > 1 && span > 0 ? ((ts[i] - t0) / span) * chartW : chartW / 2)
              const yAt = (v: number) => chartH - padY - ((v - minV) / range) * (chartH - padY * 2)
              const sparkPath = pts.map((v: number, i: number) => `${i === 0 ? 'M' : 'L'}${xAt(i).toFixed(1)},${yAt(v).toFixed(1)}`).join(' ')
              const refTopY = hasRange ? yAt(lab.refHigh as number) : 0
              const refBotY = hasRange ? yAt(lab.refLow as number) : 0
              const rangeText = labRangeText(lab)

              return (
                /* A button: the tile is the only route to the full lab-trend
                   chart, and the sparkline tooltip beside it was mouse-only
                   too. Both now open on :focus-visible as well as :hover. */
                <button
                  type="button"
                  key={`${lab.name}|${lab.unit}|${idx}`}
                  className="lab-trend-block"
                  onClick={() => setSelectedLabForGraph(lab)}
                  aria-label={`Open trend chart for ${lab.name}, currently ${lab.current}${lab.unit || ''}, ${trendMeaning}`}
                >
                  <span className="lab-trend-block__inner" style={{ borderLeft: `3px solid ${statusColor}` }}>
                    <span className="lab-trend-block__name">{lab.name}{lab.simulated && <> <SimulatedBadge /></>}</span>
                    <span className="lab-trend-block__value" style={{ color: statusText }}>
                      {lab.current}{lab.unit ? <span className="lab-trend-block__unit">{lab.unit}</span> : null}
                      <span
                        className="lab-trend-block__arrow"
                        data-trend-tone={trendTone}
                        title={trendMeaning}
                        style={{ color: LAB_TREND_TONE_COLOR[trendTone] }}
                      >{trendArrow}</span>
                    </span>
                    <span className="lab-trend-block__ref">{rangeText}</span>
                  </span>
                  {/* Hover chart tooltip */}
                  <span className="lab-trend-tooltip">
                    <span className="lab-trend-tooltip__header">
                      <span>{lab.name}</span>
                      <span style={{ fontSize: '0.65rem', color: 'var(--text-muted)' }}>{lab.unit}</span>
                    </span>
                    <svg width={chartW} height={chartH} viewBox={`0 0 ${chartW} ${chartH}`} style={{ display: 'block' }}>
                      {hasRange && (
                        <>
                          <rect x="0" y={Math.max(0, refTopY)} width={chartW} height={Math.max(1, refBotY - refTopY)} fill="color-mix(in srgb, var(--status-ok) 10%, transparent)" rx="2" />
                          <line x1="0" y1={refTopY} x2={chartW} y2={refTopY} stroke="color-mix(in srgb, var(--status-ok) 28%, transparent)" strokeWidth="1" strokeDasharray="3,3" />
                          <line x1="0" y1={refBotY} x2={chartW} y2={refBotY} stroke="color-mix(in srgb, var(--status-ok) 28%, transparent)" strokeWidth="1" strokeDasharray="3,3" />
                        </>
                      )}
                      <path d={sparkPath} fill="none" stroke={lab.status === 'unranged' ? 'var(--text-secondary)' : statusColor} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                      {pts.map((v: number, i: number) => (
                        <circle key={i} cx={xAt(i)} cy={yAt(v)} r={i === pts.length - 1 ? 3.5 : 2} fill={i === pts.length - 1 ? (lab.status === 'unranged' ? 'var(--text-secondary)' : statusColor) : 'var(--text-muted)'} stroke={i === pts.length - 1 ? 'var(--bg-elevated)' : 'none'} strokeWidth="1.5" />
                      ))}
                    </svg>
                    <span className="lab-trend-tooltip__footer">
                      <span style={{ color: statusText, fontWeight: 700 }}>{lab.current} {lab.unit}</span>
                      <span style={{ color: 'var(--text-muted)', fontSize: '0.62rem' }}>{formatClinicalDateTime(parseUTC(lab.times[lab.times.length - 1]))}</span>
                    </span>
                  </span>
                </button>
              )
            })}
          </div>
          )}
        </div>
      </div>

      {/* ═══ Sage's drafted assessment of the critical alert ═══
          DELIBERATELY BELOW the risk panel, in the DOM and on the screen. The
          alert — Orb's deterministic NEWS2 band, its drivers and the vitals in
          the command bar — is on this chart in ~51 ms and owes Sage nothing.
          The draft is a separate row the backend writes after the alert is
          already visible, and it arrives when it arrives. Nothing above this
          line waits for it, and when there is no draft this component renders
          nothing at all, so the alert simply stands alone.

          `risk.level` is passed for ONE purpose: deciding whether a failed
          read is worth reporting. It never gates the draft — a patient whose
          band has since improved still has an assessment somebody must
          resolve. */}
      <SageAlertAssessment
        patientId={patientId}
        riskLevel={risk?.level}
        role={role}
        /* Reports whether Sage's block is showing the alert's Tier 1
           breakdown. When it is, the risk panel above stands its own live copy
           down: two deterministic decompositions of two different moments,
           stacked on one chart, is a clinician being asked to work out which
           "why NEWS2" belongs to which observation. */
        onTier1Shown={setTier1OnScreen}
      />

      {/* Discharge Modal
          The drafted summary is shown HERE, to the doctor, labelled as an
          unreviewed draft; the patient's portal and discharge link withhold it
          until she releases it. The doctor used to see only the print view's
          standing prose and never the text the patient received. */}
      {dischargeModal && (
        <ModalOverlay onClose={() => !discharging && !reviewing && setDischargeModal(false)}>
          <div className="modal glass-card" role="dialog" aria-modal="true" aria-labelledby="discharge-modal-title" onClick={e => e.stopPropagation()}>
            <h2 className="modal__title" id="discharge-modal-title">Discharge {patient.name}</h2>
            {!dischargeSummary ? (
              <div className="modal__body">
                <p className="modal__text">
                  This drafts a discharge summary from the recorded chart for you to read,
                  marks the patient discharged and closes the admission. A secure link and
                  QR code are issued only when the patient has a date of birth on file.
                </p>
                <div className="modal__patient-summary">
                  <div><strong>Diagnosis:</strong> {patient.diagnosis || 'Not documented'}</div>
                  <div><strong>Ward:</strong> {patient.ward} · Bed {patient.bed}</div>
                  <div><strong>Admitted:</strong> {formatUTCDate(patient.admission_date)}</div>
                  <div><strong>Current NEWS2:</strong> {risk.level}{typeof risk.raw_news2 === 'number' ? ` (score ${risk.raw_news2})` : ''}</div>
                </div>
                {dischargeAdvisory && typeof dischargeAdvisory === 'object' && (
                  <div data-testid="discharge-billing-advisory" style={{ marginTop: 12, padding: '10px 12px', borderRadius: 8, border: `1px solid ${dischargeAdvisory.advisory_status === 'clear' ? 'var(--border-subtle)' : 'var(--status-warn)'}`, fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
                    <div style={{ fontWeight: 700, color: dischargeAdvisory.advisory_status === 'clear' ? 'var(--text-primary)' : 'var(--status-warn-text)' }}>
                      Billing: {dischargeAdvisory.headline}
                    </div>
                    {Array.isArray(dischargeAdvisory.reasons) && dischargeAdvisory.reasons.length > 0 && (
                      <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
                        {dischargeAdvisory.reasons.map((r, i) => <li key={i}>{r}</li>)}
                      </ul>
                    )}
                    <div style={{ marginTop: 6 }}>{dischargeAdvisory.message}</div>
                  </div>
                )}
                {openEscalations.length > 0 && (
                  <div data-testid="discharge-open-escalations" style={{ marginTop: 12, padding: '10px 12px', borderRadius: 8, border: '1px solid var(--status-warn)', fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
                    <div style={{ fontWeight: 700, color: 'var(--status-warn-text)' }}>
                      {openEscalations.length === 1 ? '1 deterioration escalation is still open' : `${openEscalations.length} deterioration escalations are still open`}
                    </div>
                    <div style={{ marginTop: 4 }}>Close each with a reason. This does not prevent the discharge.</div>
                    {openEscalations.map(e => {
                      const reason = escalationReasons[e.id] || ''
                      const ready = reason.trim().length >= 3 && closingEscalation === null
                      return (
                        <div key={e.id} style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 6 }}>
                          <div style={{ color: 'var(--text-primary)' }}>
                            Raised {formatUTCDateTime(e.raised_at)}
                            {typeof e.peak_news2 === 'number' ? `, highest NEWS2 ${e.peak_news2}` : ''}
                            {e.status === 'acknowledged' ? ', acknowledged' : ', not acknowledged'}
                          </div>
                          <label htmlFor={`esc-reason-${e.id}`} style={{ fontSize: '0.78rem' }}>Reason for closing</label>
                          <input
                            id={`esc-reason-${e.id}`}
                            type="text"
                            value={reason}
                            maxLength={300}
                            placeholder="For example: reviewed, improved, fit for discharge"
                            onChange={ev => setEscalationReasons(prev => ({ ...prev, [e.id]: ev.target.value }))}
                            style={{ minHeight: 32, padding: '4px 8px', borderRadius: 6, border: '1px solid var(--border-glass)', background: 'transparent', color: 'var(--text-primary)', font: 'inherit' }}
                          />
                          <button
                            type="button"
                            className="modal__cancel"
                            onClick={() => closeEscalation(e.id)}
                            disabled={!ready}
                            style={ready ? { alignSelf: 'flex-start', minHeight: 32 } : { alignSelf: 'flex-start', minHeight: 32, background: 'var(--btn-disabled-bg)', color: 'var(--btn-disabled-text)', border: '1px solid var(--btn-disabled-border)', cursor: 'not-allowed' }}
                          >
                            {closingEscalation === e.id ? 'Closing…' : 'Close escalation'}
                          </button>
                        </div>
                      )
                    })}
                    {escalationError && (
                      <div role="alert" style={{ marginTop: 8, color: 'var(--status-danger-text)' }}>{escalationError}</div>
                    )}
                  </div>
                )}
                {dischargeError && (
                  <div role="alert" style={{ marginTop: 12, padding: '10px 12px', borderRadius: 8, fontSize: '0.85rem', color: 'var(--status-danger-text)', border: '1px solid var(--status-danger)' }}>
                    {dischargeError}
                  </div>
                )}
                <div className="modal__actions">
                  <button className="modal__cancel" onClick={() => setDischargeModal(false)} disabled={discharging}>Cancel</button>
                  <button ref={dischargePrimaryBtnRef} className="modal__confirm" onClick={handleDischarge} disabled={discharging}>
                    {discharging ? 'Drafting summary…' : 'Confirm discharge'}
                  </button>
                </div>
              </div>
            ) : (
              <div className="modal__body">
                <div className="modal__success">Patient discharged</div>
                <div className="modal__summary-content" data-testid="discharge-draft">
                  <h3 style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                    Discharge summary
                    <span style={{ fontSize: '0.72rem', fontWeight: 700, padding: '2px 8px', borderRadius: 'var(--radius-full)', border: `1px solid ${dischargeSummary.review_status === 'reviewed' ? 'var(--status-ok)' : 'var(--status-warn)'}`, color: dischargeSummary.review_status === 'reviewed' ? 'var(--status-ok-text)' : 'var(--status-warn-text)' }}>
                      {dischargeSummary.review_status === 'reviewed'
                        ? `Reviewed by ${dischargeSummary.reviewed_by_name || 'a clinician'}, released to the patient`
                        : dischargeSummary.drafted_by === 'unavailable'
                          ? 'Recorded data only, not reviewed'
                          : 'AI-drafted, not reviewed'}
                    </span>
                  </h3>
                  {dischargeSummary.review_status !== 'reviewed' && (
                    <p className="modal__text" style={{ marginTop: 4 }}>
                      {dischargeSummary.drafted_by === 'unavailable'
                        ? 'The drafting assistant was unavailable, so this holds the recorded data only. '
                        : 'Drafted by the assistant from the recorded chart. Check it against the chart. '}
                      The patient sees it only after you release it.
                    </p>
                  )}
                  <div className="discharge-preview" style={{ whiteSpace: 'pre-wrap' }}>{dischargeSummary.summary || 'Not documented'}</div>
                </div>
                {dischargeSummary.review_status !== 'reviewed' && canReleaseSummary && dischargeSummary.summary_id && (
                  <div className="modal__actions" style={{ justifyContent: 'flex-start' }}>
                    <button className="modal__confirm" onClick={handleReviewSummary} disabled={reviewing}>
                      {reviewing ? 'Releasing…' : 'I have read this summary: release it to the patient'}
                    </button>
                  </div>
                )}
                {dischargeSummary.timeline?.length > 0 && (
                  <div className="modal__timeline">
                    <h3>Timeline</h3>
                    {dischargeSummary.timeline.map((event: any, i: number) => (
                      <div key={i} className="modal__timeline-event">
                        <div className={`modal__timeline-dot modal__timeline-dot--${event.type}`} />
                        <div className="modal__timeline-info">
                          <span className="modal__timeline-title">{event.title}</span>
                          <span className="modal__timeline-detail">{event.detail}</span>
                          <span className="modal__timeline-date">{formatUTCDateTime(event.timestamp)}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
                {dischargeSummary.qr_code_url ? (
                  <div className="modal__qr">
                    <h3>Discharge QR Code</h3>
                    {dischargeQrSrc ? (
                      <img
                        src={dischargeQrSrc}
                        alt="Discharge QR Code"
                        className="modal__qr-image"
                      />
                    ) : (
                      <div className="modal__qr-image" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', minHeight: 120, color: 'var(--text-muted)', fontSize: '0.8rem' }}>Loading QR…</div>
                    )}
                    {/* Ruling 2026-10-04 (option A): the link never opens from
                        outside the hospital, so the handover happens here. */}
                    <p className="modal__qr-hint" data-testid="discharge-qr-hint">
                      Before the patient leaves: release the summary to them, then have them scan this on the hospital&apos;s patient Wi-Fi, unlock it with their date of birth and save a copy. It does not open outside the hospital.
                    </p>
                  </div>
                ) : dischargeSummary.secure_link_issued === false ? (
                  <div className="modal__qr" data-testid="discharge-no-link">
                    <h3>No secure link issued</h3>
                    <p className="modal__text">{dischargeSummary.secure_link_reason || 'The server did not give a reason.'}</p>
                  </div>
                ) : null}
                {dischargeError && (
                  <div role="alert" style={{ marginTop: 12, padding: '10px 12px', borderRadius: 8, fontSize: '0.85rem', color: 'var(--status-danger-text)', border: '1px solid var(--status-danger)' }}>
                    {dischargeError}
                  </div>
                )}
                <div className="modal__actions" style={{ display: 'flex', gap: '12px', justifyContent: 'flex-end', width: '100%', flexWrap: 'wrap' }}>
                  <button className="modal__cancel" onClick={() => setPrintType('discharge')}>Print summary</button>
                  <button className="modal__cancel" onClick={() => setPrintType('discharge-timeline')}>Print with timeline</button>
                  <button ref={dischargePrimaryBtnRef} className="modal__confirm" onClick={() => { setDischargeModal(false); onBack() }} disabled={reviewing}>Done</button>
                </div>
              </div>
              )}
          </div>
        </ModalOverlay>
      )}

      {/* Note Modal */}
      {selectedNote && (
        <ModalOverlay onClose={() => setSelectedNote(null)}>
          <div className="modal glass-card" role="dialog" aria-modal="true" aria-labelledby="note-modal-title" style={{ maxWidth: 640 }} onClick={e => e.stopPropagation()}>
            <h2 className="modal__title" id="note-modal-title" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
              Clinical Note
              <span className={`detail__note-badge detail__note-badge--${selectedNote.doc_type || 'scribe'}`}>
                {selectedNote.doc_type || selectedNote.note_format}
              </span>
            </h2>
            <div className="modal__body" style={{ maxHeight: '60vh', overflowY: 'auto', paddingRight: 10 }}>
              <div className="detail__note-date" style={{ marginBottom: 16 }}>{formatUTCDateTime(selectedNote.created_at)}</div>
              <p className="modal__text" style={{ whiteSpace: 'pre-wrap', lineHeight: 1.6 }}>{selectedNote.structured_note || selectedNote.raw_transcript || selectedNote.content}</p>
            </div>
            <div className="modal__actions">
              <button className="modal__btn modal__btn--ghost" style={{ padding: '8px 16px', borderRadius: 8 }} onClick={() => setSelectedNote(null)}>Close</button>
            </div>
          </div>
        </ModalOverlay>
      )}

      {/* Report Modal — handles both diagnostic imaging and lab reports */}
      {selectedReport && (() => {
        const isLabReport = !selectedReport.image_path || selectedReport.image_type === 'lab'
        return (
        <ModalOverlay onClose={() => { setSelectedReport(null); if (reportOpenedFrom === 'diag') setDiagReportsModalOpen(true); else if (reportOpenedFrom === 'lab') setLabReportsModalOpen(true); setReportOpenedFrom(null); }}>
          <div className="modal glass-card" role="dialog" aria-modal="true" aria-labelledby="report-modal-title" style={{ maxWidth: isLabReport ? 700 : 840, width: '100%' }} onClick={e => e.stopPropagation()}>
            <h2 className="modal__title" id="report-modal-title" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
              <Icon name={isLabReport ? 'flask' : 'image'} size={18} />
              {isLabReport ? 'Lab Report' : 'Diagnostic Report'}
              <span className={`detail__note-badge detail__note-badge--${selectedReport.image_type?.toUpperCase()}`} style={{ border: '1px solid var(--border-glass)' }}>
                {isLabReport
                  ? (selectedReport.clinical_context?.replace(/^Lab:\s*/i, '') || selectedReport.title || 'Lab')
                  : selectedReport.image_type?.toUpperCase()
                }
              </span>
              {(selectedReport.analysis || selectedReport.content || '').includes('CRITICAL') && (
                <span className="reports-modal__critical-badge"><Icon name="alert" size={12} /> CRITICAL</span>
              )}
              {selectedReport.source === 'simulated' && <SimulatedBadge what={isLabReport ? 'result' : 'report'} />}
            </h2>
            <div className="modal__body" style={{ display: 'flex', gap: 32, alignItems: 'flex-start', maxHeight: '70vh', overflowY: 'auto' }}>
              {/* image_url is the authenticated /uploads path; image_path is a
                  location on the appliance's disk and never loaded in a browser,
                  which is why a filed upload rendered as a broken image. */}
              {!isLabReport && selectedReport.image_url && (
                <img src={selectedReport.image_url} alt="Medical scan" style={{ width: '45%', borderRadius: 8, objectFit: 'contain', background: 'var(--bg-elevated)', border: '1px solid var(--border-glass)' }} />
              )}
              <div style={{ flex: 1 }}>
                <div className="detail__note-date" style={{ marginBottom: 16 }}>{formatUTCDateTime(selectedReport.created_at)}</div>
                <h4 style={{ color: 'var(--text-secondary)', marginBottom: 8, fontSize: '0.85rem', letterSpacing: '0.05em' }}>
                  {isLabReport ? 'LAB RESULTS' : selectedReport.reviewed_text ? 'CLINICIAN-REVIEWED FINDINGS' : 'AI FINDINGS, NOT REVIEWED BY A CLINICIAN'}
                </h4>
                {/* A filed read shows the text the clinician confirmed. The raw
                    model read is shown only for a row nobody reviewed, and says so. */}
                <p className="modal__text" style={{ whiteSpace: 'pre-wrap', lineHeight: 1.6, fontSize: '0.9rem', color: 'var(--text-primary)' }}>{(!isLabReport && selectedReport.reviewed_text) || selectedReport.analysis || selectedReport.content}</p>
                {!isLabReport && selectedReport.reviewed_at && (
                  <div className="detail__note-date" style={{ marginTop: 12 }}>Reviewed and filed {formatUTCDateTime(selectedReport.reviewed_at)}</div>
                )}
              </div>
            </div>
            <div className="modal__actions">
              <button className="modal__btn modal__btn--ghost" style={{ padding: '8px 16px', borderRadius: 8 }} onClick={() => { setSelectedReport(null); if (reportOpenedFrom === 'diag') setDiagReportsModalOpen(true); else if (reportOpenedFrom === 'lab') setLabReportsModalOpen(true); setReportOpenedFrom(null); }}>Close</button>
            </div>
          </div>
        </ModalOverlay>
        )
      })()}

      {/* Notes History Modal */}
      {notesHistoryModalOpen && (
        <ModalOverlay onClose={() => setNotesHistoryModalOpen(false)}>
          <div className="modal glass-card" role="dialog" aria-modal="true" aria-labelledby="notes-history-modal-title" style={{ maxWidth: 840, width: '100%' }} onClick={e => e.stopPropagation()}>
            <h2 className="modal__title" id="notes-history-modal-title" style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              Clinical Notes History
              {/* The only way to write a note from the chart. The Add Clinical
                  Note modal existed with nothing that opened it. */}
              {canWriteNote && (
                <button
                  type="button"
                  className="modal__btn"
                  style={{ marginLeft: 'auto', padding: '6px 12px', fontSize: '0.8rem', minHeight: 24 }}
                  onClick={() => { setNotesHistoryModalOpen(false); openAddNote() }}
                >
                  Add note
                </button>
              )}
            </h2>
            <div className="modal__body" style={{ maxHeight: '70vh', display: 'flex', flexDirection: 'column' }}>
              <div className="search-wrapper">
                <input 
                   type="text" 
                   className="search-input" 
                   aria-label="Search notes"
                   placeholder="Search by word, type or date (e.g. 1 Oct 2026)"
                   value={notesSearchQuery}
                   onChange={(e) => setNotesSearchQuery(e.target.value)}
                />
              </div>
              <div className="detail__notes-list" style={{ overflowY: 'auto' }}>
                {!filteredHistoryNotes?.length ? (
                  <div className="detail__no-data">No clinical notes match search</div>
                ) : (
                  filteredHistoryNotes.map((n: any, i: number) => (
                    /* A <button>, not a <div onClick>. These cards opened a note
                       with no cursor change, no hover and no keyboard path —
                       the only list in this stylesheet that hid the fact it was
                       interactive, while .detail__report and
                       .reports-modal__card right beside it advertised it. */
                    <button
                      type="button"
                      key={`hist-note-${i}`}
                      className="detail__note"
                      onClick={() => { setSelectedNote(n); }}
                    >
                      <span className="detail__note-header">
                        <span className={`detail__note-badge detail__note-badge--${n.doc_type || 'scribe'}`}>{n.doc_type || `scribe · ${n.note_format}`}</span>
                        <span className="detail__note-date">{formatUTCDate(n.created_at)} {formatUTCTime(n.created_at)}</span>
                      </span>
                      <span className="detail__note-content">{n.structured_note || n.raw_transcript || n.content}</span>
                    </button>
                  ))
                )}
              </div>
            </div>
            <div className="modal__actions">
              <button className="modal__btn modal__btn--ghost" style={{ padding: '8px 16px', borderRadius: 8 }} onClick={() => setNotesHistoryModalOpen(false)}>Close</button>
            </div>
          </div>
        </ModalOverlay>
      )}

      {/* ═══ Diag Reports Modal ═══ */}
      {diagReportsModalOpen && (
        <ModalOverlay onClose={() => setDiagReportsModalOpen(false)}>
          <div className="modal glass-card" role="dialog" aria-modal="true" aria-labelledby="diag-reports-modal-title" style={{ maxWidth: 900, width: '100%' }} onClick={e => e.stopPropagation()}>
            <h2 className="modal__title" id="diag-reports-modal-title" style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--status-warn)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
              Diagnostic Reports
              <span style={{ fontSize: '0.75rem', fontWeight: 400, color: 'var(--text-muted)', marginLeft: 'auto' }}>{allDiagnosticReports.length} total</span>
            </h2>
            <div className="modal__body" style={{ maxHeight: '72vh', display: 'flex', flexDirection: 'column' }}>
              <div className="reports-modal__search">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true" style={{ color: 'var(--text-muted)', flexShrink: 0 }}><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                <input 
                   type="text" 
                   className="reports-modal__search-input" 
                   aria-label="Search diagnostic reports"
                   placeholder="Search by keyword or date (e.g. 1 Oct 2026)"
                   value={diagSearchQuery}
                   onChange={(e) => setDiagSearchQuery(e.target.value)}
                   autoFocus
                />
                {diagSearchQuery && (
                  <button className="reports-modal__clear" aria-label="Clear search" onClick={() => setDiagSearchQuery('')}>✕</button>
                )}
              </div>
              <div className="reports-modal__list">
                {!filteredDiagReports?.length ? (
                  <div className="reports-modal__empty">
                    <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1" style={{ opacity: 0.2 }}><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
                    <span>No diagnostic reports {diagSearchQuery ? 'match your search' : 'available'}</span>
                  </div>
                ) : (
                  filteredDiagReports.map((a: any, i: number) => (
                    /* A <button>: the card IS the only way into a report, and a
                       chevron drawn on a <div> is not a keyboard affordance. */
                    <button
                      type="button"
                      key={`diag-modal-${i}`}
                      className="reports-modal__card"
                      onClick={() => { setSelectedReport(a); setDiagReportsModalOpen(false); setReportOpenedFrom('diag'); }}
                    >
                      <span className="reports-modal__card-icon reports-modal__card-icon--diag" aria-hidden>
                        <Icon name="image" size={18} />
                      </span>
                      <span className="reports-modal__card-body">
                        <span className="reports-modal__card-header">
                          <span className="reports-modal__card-type">{a.image_type?.toUpperCase()}</span>
                          {a.source === 'simulated' && <SimulatedBadge what="report" />}
                          <span className="reports-modal__card-date">{formatUTCDate(a.created_at, 'en-GB', { day: '2-digit', month: 'short', year: 'numeric' })} · {formatUTCTime(a.created_at)}</span>
                        </span>
                        <span className="reports-modal__card-context">{a.clinical_context || 'AI Monitored Scan'}</span>
                        <span className="reports-modal__card-preview">{(a.reviewed_text || a.analysis || '').substring(0, 120)}…</span>
                      </span>
                      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ opacity: 0.3, flexShrink: 0 }} aria-hidden><polyline points="9 18 15 12 9 6"/></svg>
                    </button>
                  ))
                )}
              </div>
            </div>
            <div className="modal__actions">
              <button className="modal__btn modal__btn--ghost" style={{ padding: '8px 16px', borderRadius: 8 }} onClick={() => setDiagReportsModalOpen(false)}>Close</button>
            </div>
          </div>
        </ModalOverlay>
      )}

      {/* ═══ Lab Reports Modal ═══ */}
      {labReportsModalOpen && (
        <ModalOverlay onClose={() => setLabReportsModalOpen(false)}>
          <div className="modal glass-card" role="dialog" aria-modal="true" aria-labelledby="lab-reports-modal-title" style={{ maxWidth: 900, width: '100%' }} onClick={e => e.stopPropagation()}>
            <h2 className="modal__title" id="lab-reports-modal-title" style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--status-danger)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 3h6v7l4 9H5l4-9V3z"/><line x1="9" y1="3" x2="15" y2="3"/></svg>
              Lab Reports
              <span style={{ fontSize: '0.75rem', fontWeight: 400, color: 'var(--text-muted)', marginLeft: 'auto' }}>{allLabReports.length} total</span>
            </h2>
            <div className="modal__body" style={{ maxHeight: '72vh', display: 'flex', flexDirection: 'column' }}>
              <div className="reports-modal__search">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true" style={{ color: 'var(--text-muted)', flexShrink: 0 }}><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
                <input 
                   type="text" 
                   className="reports-modal__search-input" 
                   aria-label="Search lab reports"
                   placeholder="Search by keyword or date (e.g. 1 Oct 2026)"
                   value={labSearchQuery}
                   onChange={(e) => setLabSearchQuery(e.target.value)}
                   autoFocus
                />
                {labSearchQuery && (
                  <button className="reports-modal__clear" aria-label="Clear search" onClick={() => setLabSearchQuery('')}>✕</button>
                )}
              </div>
              <div className="reports-modal__list">
                {!filteredLabReports?.length ? (
                  <div className="reports-modal__empty">
                    <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1" style={{ opacity: 0.2 }}><path d="M9 3h6v7l4 9H5l4-9V3z"/><line x1="9" y1="3" x2="15" y2="3"/></svg>
                    <span>No lab reports {labSearchQuery ? 'match your search' : 'available'}</span>
                  </div>
                ) : (
                  filteredLabReports.map((a: any, i: number) => {
                    const hasCritical = (a.analysis || a.content || '').includes('CRITICAL')
                    return (
                      <button
                        type="button"
                        key={`lab-modal-${i}`}
                        className={`reports-modal__card ${hasCritical ? 'reports-modal__card--critical' : ''}`}
                        onClick={() => { setSelectedReport(a); setLabReportsModalOpen(false); setReportOpenedFrom('lab'); }}
                      >
                        <span className={`reports-modal__card-icon reports-modal__card-icon--lab ${hasCritical ? 'reports-modal__card-icon--critical' : ''}`} aria-hidden>
                          <Icon name="flask" size={18} />
                        </span>
                        <span className="reports-modal__card-body">
                          <span className="reports-modal__card-header">
                            <span className="reports-modal__card-type">{a.clinical_context?.replace(/^Lab:\s*/i, '') || a.title || 'Lab Report'}</span>
                            {hasCritical && <span className="reports-modal__critical-badge"><Icon name="alert" size={12} /> CRITICAL</span>}
                            {a.source === 'simulated' && <SimulatedBadge />}
                            <span className="reports-modal__card-date">{formatUTCDate(a.created_at, 'en-GB', { day: '2-digit', month: 'short', year: 'numeric' })} · {formatUTCTime(a.created_at)}</span>
                          </span>
                          <span className="reports-modal__card-preview" style={{ whiteSpace: 'pre-wrap' }}>{(a.analysis || a.content || '').substring(0, 180)}…</span>
                        </span>
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ opacity: 0.3, flexShrink: 0 }} aria-hidden><polyline points="9 18 15 12 9 6"/></svg>
                      </button>
                    )
                  })
                )}
              </div>
            </div>
            <div className="modal__actions">
              <button className="modal__btn modal__btn--ghost" style={{ padding: '8px 16px', borderRadius: 8 }} onClick={() => setLabReportsModalOpen(false)}>Close</button>
            </div>
          </div>
        </ModalOverlay>
      )}

      {/* Add Note Modal */}
      {addNoteModalOpen && (
        <ModalOverlay onClose={() => !isSubmitting && setAddNoteModalOpen(false)}>
          <div className="modal glass-card" role="dialog" aria-modal="true" aria-labelledby="add-note-modal-title" style={{ maxWidth: 640 }} onClick={e => e.stopPropagation()}>
            <h2 className="modal__title" id="add-note-modal-title">Add Clinical Note</h2>
            <div className="modal__body">
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px', marginBottom: '16px', fontSize: '0.85rem', color: 'var(--text-secondary)' }}>
                <div><strong>Patient:</strong> {patient.name}</div>
                <div><strong>Date/Time:</strong> {formatClinicalDateTime(new Date())}</div>
              </div>
              <textarea 
                className="sage-input" 
                aria-label="Clinical note"
                style={{ width: '100%', height: '150px', background: 'var(--bg-glass)', border: '1px solid var(--border-subtle)', color: 'var(--text-primary)', padding: '12px', borderRadius: '8px', fontSize: '0.9rem', resize: 'vertical' }}
                placeholder="Enter clinical note here..." 
                value={newNote} 
                onChange={(e) => setNewNote(e.target.value)} 
                disabled={isSubmitting}
              />
              {noteError && (
                <div role="alert" style={{ marginTop: 12, padding: '10px 12px', borderRadius: 8, fontSize: '0.85rem', color: 'var(--status-danger-text)', border: '1px solid var(--status-danger)' }}>
                  {noteError}
                </div>
              )}
              <div style={{ marginTop: '16px', fontSize: '0.85rem', color: 'var(--text-muted)' }}>
                <strong>Signed By:</strong> {currentUser?.name || 'Not recorded'}
              </div>
            </div>
            <div className="modal__actions">
              <button className="modal__cancel" onClick={() => setAddNoteModalOpen(false)} disabled={isSubmitting}>Cancel</button>
              <button className="modal__confirm" onClick={handleAddNote} disabled={isSubmitting || !newNote.trim()}>
                {isSubmitting ? 'Saving...' : 'Save Note'}
              </button>
            </div>
          </div>
        </ModalOverlay>
      )}

      
      {/* ═══ Medications Modal ═══ */}
      {medsListModalOpen && (
        <ModalOverlay onClose={() => setMedsListModalOpen(false)}>
          <div className="modal glass-card" role="dialog" aria-modal="true" aria-labelledby="meds-list-modal-title" style={{ maxWidth: 800, width: '100%' }} onClick={e => e.stopPropagation()}>
            <h2 className="modal__title" id="meds-list-modal-title" style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M10.5 1.5H8C4.68629 1.5 2 4.18629 2 7.5C2 10.8137 4.68629 13.5 8 13.5H10.5"/><path d="M13.5 1.5H16C19.3137 1.5 22 4.18629 22 7.5C22 10.8137 19.3137 13.5 16 13.5H13.5"/><line x1="8" y1="7.5" x2="16" y2="7.5"/></svg>
              Medications
              <span style={{ fontSize: '0.75rem', fontWeight: 400, color: 'var(--text-muted)', marginLeft: 'auto' }}>{medications?.length || 0} total</span>
              <button className="detail__add-btn" title="Add Medication" style={{ background: 'transparent', border: 'none', color: 'var(--text-primary)', cursor: 'pointer', padding: '4px' }} onClick={() => { setMedError(null); setAddMedModalOpen(true) }}>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
              </button>
            </h2>
            <div className="modal__body" style={{ maxHeight: '72vh', overflowY: 'auto' }}>
              <div className="detail__meds-list" style={{ marginTop: 8 }}>
                {!medications?.length ? (
                  <div className="detail__no-data">No medications prescribed</div>
                ) : (
                  <table className="detail__meds-table">
                    <thead>
                      <tr>
                        <th>Drug</th>
                        <th>Dose</th>
                        <th>Route</th>
                        <th>Frequency</th>
                        <th>Status</th>
                        <th>Administration</th>
                      </tr>
                    </thead>
                    <tbody>
                      {medications.map((m: any, i: number) => {
                        // Newest-first history: first 'given' row for this med is the latest dose.
                        const lastGiven = administrations.find(a => a.medication_id === m.id && a.status === 'given')
                        return (
                        <tr key={i} className={`detail__med-row detail__med-row--${m.status}`}>
                          <td>
                            <span className="detail__med-name">{m.drug_name}</span>
                            {m.notes && <span className="detail__med-note">{m.notes}</span>}
                          </td>
                          <td className="detail__med-dose metric-mono">{m.dosage}</td>
                          <td>
                            <span className={`detail__med-route detail__med-route--${m.route?.toLowerCase().replace(/\s+/g, '-')}`}>
                              {m.route}
                            </span>
                          </td>
                          <td className="detail__med-freq">{m.frequency}</td>
                          <td>
                            <span className={`detail__med-status detail__med-status--${m.status}`}>
                              {m.status === 'active' ? '● Active' : m.status === 'prn' ? '◌ PRN' : '○ D/C'}
                            </span>
                          </td>
                          <td>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                              {lastGiven && (
                                <span
                                  className="metric-mono"
                                  title={`Last dose recorded by ${lastGiven.administered_by_name || lastGiven.administered_by}`}
                                  style={{ fontSize: '0.7rem', fontWeight: 700, letterSpacing: '0.03em', color: 'var(--status-ok)', border: '1px solid var(--status-ok)', borderRadius: 'var(--radius-full)', padding: '2px 8px', whiteSpace: 'nowrap' }}
                                >
                                  Given {formatAdministeredTime(lastGiven.administered_at)}
                                </span>
                              )}
                              {/* NEVER SILENT. A dose given ACROSS a charted
                                  allergy is marked wherever the administration
                                  is shown, with the written reason on hover —
                                  a colleague reading this chart later must not
                                  have to go to the audit log to find out that
                                  the interlock was cleared, or why. Danger text
                                  on a matching border, no opacity. */}
                              {givenAcrossAllergy(lastGiven) && (
                                <span
                                  title={`Given despite a charted allergy: ${lastGiven!.allergy_check?.override?.reason ?? 'reason recorded in the audit trail'}`}
                                  style={{ fontSize: '0.7rem', fontWeight: 700, letterSpacing: '0.03em', color: 'var(--status-danger-text)', border: '1px solid var(--status-danger)', borderRadius: 'var(--radius-full)', padding: '2px 8px', whiteSpace: 'nowrap' }}
                                >
                                  Across charted allergy
                                </span>
                              )}
                              {m.status === 'active' && (
                                <button
                                  className="modal__btn"
                                  disabled={!!m.unverified}
                                  title={m.unverified ? 'Pharmacist verification required' : `Record administration of ${m.drug_name}`}
                                  // No opacity here. It used to be `opacity: m.unverified ? 0.5 : 1`,
                                  // which composited the label down to 0.25 alpha — 2.07:1 dark,
                                  // 1.65:1 light — so a nurse could not read the button that was
                                  // withheld pending pharmacist verification. Inline styles beat
                                  // stylesheets, which is why it survived the CSS sweep. The
                                  // disabled treatment now comes from .modal__btn:disabled in
                                  // Vigil.css (flat neutral fill + readable label).
                                  style={{ padding: '4px 10px', fontSize: '0.72rem', border: '1px solid var(--border-glass)', background: 'var(--bg-glass)', color: 'var(--text-primary)' }}
                                  onClick={() => setAdministerTarget(m)}
                                >
                                  Administer
                                </button>
                              )}
                            </div>
                          </td>
                        </tr>
                        )
                      })}
                    </tbody>
                  </table>
                )}
              </div>
            </div>
            <div className="modal__actions" style={{ marginTop: 16 }}>
              <button className="modal__btn modal__btn--ghost" style={{ padding: '8px 16px', borderRadius: 8 }} onClick={() => setMedsListModalOpen(false)}>Close</button>
            </div>
          </div>
        </ModalOverlay>
      )}

      
      {/* ═══ Detailed Lab Graph Modal ═══
          Every point is a recorded result plotted at the time it was
          observed, on a time axis: the axis used to label the points
          T-0H, T-6H, T-12H… whatever their real spacing. */}
      {selectedLabForGraph && (() => {
        const lab = selectedLabForGraph as LabTrend
        const hasRange = lab.refLow !== null && lab.refHigh !== null
        const lineColor = lab.status === 'critical' ? chartTheme.statusDanger
          : lab.status === 'high' || lab.status === 'abnormal' ? chartTheme.statusWarn
            : lab.status === 'low' ? chartTheme.statusInfo
              : lab.status === 'normal' ? chartTheme.statusOk : chartTheme.tick
        const latestAt = lab.times[lab.times.length - 1]
        return (
        <ModalOverlay onClose={() => setSelectedLabForGraph(null)}>
          <div className="modal glass-card" role="dialog" aria-modal="true" aria-labelledby="lab-graph-modal-title" style={{ maxWidth: 700, width: '100%', padding: '24px' }} onClick={e => e.stopPropagation()}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '20px' }}>
              <div>
                <h2 className="modal__title" id="lab-graph-modal-title" style={{ margin: 0, fontSize: '1.2rem', display: 'flex', alignItems: 'center', gap: 8 }}>
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke={LAB_STATUS_FILL[lab.status]} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
                  {lab.name} Trend
                </h2>
                <div style={{ color: 'var(--text-muted)', fontSize: '0.85rem', marginTop: 4 }}>
                  Reference range: {labRangeText(lab)}{hasRange && lab.unit ? ` ${lab.unit}` : ''}
                </div>
              </div>
              <div style={{ textAlign: 'right' }}>
                <div style={{ fontSize: '1.5rem', fontWeight: 800, color: LAB_STATUS_TEXT[lab.status] }}>
                  {lab.current} <span style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>{lab.unit}</span>
                </div>
                <div style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-muted)' }}>
                  Latest result, {formatClinicalDateTime(parseUTC(latestAt))}
                </div>
              </div>
            </div>
            
            <div style={{ width: '100%', height: 320, marginTop: '16px' }} data-testid="lab-graph">
              <SizedChart>
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={labGraphData(lab)} margin={{ top: 10, right: 10, left: -20, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke={chartTheme.grid} vertical={false} />
                  <XAxis
                    dataKey="t"
                    type="number"
                    scale="time"
                    domain={['dataMin', 'dataMax']}
                    tickFormatter={(t: number) => formatClinicalDateTime(new Date(t))}
                    stroke={chartTheme.axis} tick={{ fill: chartTheme.tick }} fontSize={11} tickMargin={8}
                  />
                  <YAxis stroke={chartTheme.axis} tick={{ fill: chartTheme.tick }} fontSize={11} tickMargin={8} domain={['auto', 'auto']} />
                  <RechartsTooltip
                    contentStyle={{ backgroundColor: chartTheme.tooltipBg, border: `1px solid ${chartTheme.tooltipBorder}`, borderRadius: '8px', color: chartTheme.tooltipText, boxShadow: '0 8px 32px rgba(0,0,0,0.4)' }}
                    itemStyle={{ color: chartTheme.tooltipText, fontWeight: 700 }}
                    labelStyle={{ color: chartTheme.tooltipText }}
                    labelFormatter={(t) => (typeof t === 'number' ? formatClinicalDateTime(new Date(t)) : '')}
                  />
                  {hasRange && (
                    <ReferenceArea y1={lab.refLow as number} y2={lab.refHigh as number} fill={chartTheme.statusOk} fillOpacity={0.08} strokeOpacity={0} />
                  )}
                  <Legend verticalAlign="top" height={36} wrapperStyle={{ fontSize: '12px', color: 'var(--text-secondary)' }} />
                  <Line
                    type="linear"
                    name={lab.unit ? `${lab.name} (${lab.unit})` : lab.name}
                    dataKey="value"
                    stroke={lineColor}
                    strokeWidth={3}
                    dot={{ r: 4, fill: chartTheme.surface, strokeWidth: 2 }}
                    activeDot={{ r: 6, strokeWidth: 0 }}
                    animationDuration={800}
                  />
                </LineChart>
              </ResponsiveContainer>
              </SizedChart>
            </div>
            
            <div className="modal__actions" style={{ marginTop: 24, justifyContent: 'flex-end' }}>
              <button className="modal__btn modal__btn--ghost" style={{ padding: '8px 24px', borderRadius: 8, fontWeight: 600 }} onClick={() => setSelectedLabForGraph(null)}>Close Tracker</button>
            </div>
          </div>
        </ModalOverlay>
        )
      })()}

      {/* Add Medication Modal */}
      {addMedModalOpen && (
        <ModalOverlay onClose={() => { if (!isSubmitting) { setAddMedModalOpen(false); setMedError(null) } }}>
          <div className="modal glass-card" role="dialog" aria-modal="true" aria-labelledby="add-med-modal-title" style={{ maxWidth: 640 }} onClick={e => e.stopPropagation()}>
            <h2 className="modal__title" id="add-med-modal-title">Add Medication</h2>
            <div className="modal__body" style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
              {medError && (
                <div role="alert" style={{ padding: '10px 12px', borderRadius: 8, fontSize: '0.85rem', lineHeight: 1.4, color: 'var(--status-danger-text)', border: '1px solid var(--status-danger)', background: 'var(--bg-glass)' }}>
                  {medError}
                </div>
              )}
              <div>
                <label htmlFor="add-med-drug" style={{ display: 'block', fontSize: '0.8rem', color: 'var(--text-secondary)', marginBottom: '4px' }}>Drug Name</label>
                <input id="add-med-drug" type="text" style={{ width: '100%', background: 'var(--bg-glass)', border: '1px solid var(--border-subtle)', color: 'var(--text-primary)', padding: '8px 12px', borderRadius: '6px' }} value={newMed.drug_name} onChange={e => setNewMed({...newMed, drug_name: e.target.value})} disabled={isSubmitting} placeholder="e.g. Paracetamol" />
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                <div>
                  <label htmlFor="add-med-dose" style={{ display: 'block', fontSize: '0.8rem', color: 'var(--text-secondary)', marginBottom: '4px' }}>Dose</label>
                  <input id="add-med-dose" type="text" style={{ width: '100%', background: 'var(--bg-glass)', border: '1px solid var(--border-subtle)', color: 'var(--text-primary)', padding: '8px 12px', borderRadius: '6px' }} value={newMed.dosage} onChange={e => setNewMed({...newMed, dosage: e.target.value})} disabled={isSubmitting} placeholder="e.g. 500mg" />
                </div>
                <div>
                  <label htmlFor="add-med-route" style={{ display: 'block', fontSize: '0.8rem', color: 'var(--text-secondary)', marginBottom: '4px' }}>Route</label>
                  <select id="add-med-route" style={{ width: '100%', background: 'var(--bg-glass)', border: '1px solid var(--border-subtle)', color: 'var(--text-primary)', padding: '8px 12px', borderRadius: '6px' }} value={newMed.route} onChange={e => setNewMed({...newMed, route: e.target.value})} disabled={isSubmitting}>
                    <option value="Oral">Oral</option>
                    <option value="IV">IV</option>
                    <option value="IV Infusion">IV Infusion</option>
                    <option value="IM">IM</option>
                    <option value="SC">SC</option>
                    <option value="Nebulizer">Nebulizer</option>
                  </select>
                </div>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                <div>
                  <label htmlFor="add-med-frequency" style={{ display: 'block', fontSize: '0.8rem', color: 'var(--text-secondary)', marginBottom: '4px' }}>Frequency</label>
                  <input id="add-med-frequency" type="text" style={{ width: '100%', background: 'var(--bg-glass)', border: '1px solid var(--border-subtle)', color: 'var(--text-primary)', padding: '8px 12px', borderRadius: '6px' }} value={newMed.frequency} onChange={e => setNewMed({...newMed, frequency: e.target.value})} disabled={isSubmitting} placeholder="e.g. QID" />
                </div>
                <div>
                  <label htmlFor="add-med-status" style={{ display: 'block', fontSize: '0.8rem', color: 'var(--text-secondary)', marginBottom: '4px' }}>Status</label>
                  <select id="add-med-status" style={{ width: '100%', background: 'var(--bg-glass)', border: '1px solid var(--border-subtle)', color: 'var(--text-primary)', padding: '8px 12px', borderRadius: '6px' }} value={newMed.status} onChange={e => setNewMed({...newMed, status: e.target.value})} disabled={isSubmitting}>
                    <option value="active">Active</option>
                    <option value="prn">PRN</option>
                    <option value="discontinued">Discontinued</option>
                  </select>
                </div>
              </div>
            </div>
            <div className="modal__actions">
              <button className="modal__cancel" onClick={() => { setAddMedModalOpen(false); setMedError(null) }} disabled={isSubmitting}>Cancel</button>
              <button className="modal__confirm" onClick={handleAddMed} disabled={isSubmitting || !newMed.drug_name || !newMed.dosage || !newMed.frequency}>
                {isSubmitting ? 'Saving...' : 'Add Medication'}
              </button>
            </div>
          </div>
        </ModalOverlay>
      )}

      {/* Drug-safety gate: blocking 428 acknowledgement / post-order info review */}
      {medSafety && (
        <SafetyAlertModal
          safety={medSafety.safety}
          mode={medSafety.mode}
          drugName={medSafety.mode === 'blocking' ? newMed.drug_name : undefined}
          busy={isSubmitting}
          onAcknowledge={(reason) => submitMedication({ acknowledged: true, reason })}
          // Info mode opens AFTER the write: say so first. No cancel is offered
          // because this lane has no way to stop a charted medication yet.
          placedNotice={medSafety.mode === 'info'
            ? 'This medication has already been added to the chart. Review the alerts below against an order that exists.'
            : undefined}
          onClose={() => setMedSafety(null)}
        />
      )}

      {/* eMAR: bedside administration (five rights + type-to-confirm) */}
      {administerTarget && (
        <AdministerModal
          med={administerTarget}
          patientName={patient.name}
          patientBed={patient.bed}
          allergies={chartedAllergies}
          onClose={() => setAdministerTarget(null)}
          onRecorded={() => { setAdministerTarget(null); loadAdministrations() }}
        />
      )}

      {storyOpen && (
        <PatientStory patientId={patientId} patientName={patient.name} onClose={() => setStoryOpen(false)} />
      )}

      {/* Print views, portalled into body so they escape the panel's
          display:none (src/print.css). Both print recorded data only: PRINT
          RECORD is the inpatient record and carries no discharge date, and the
          discharge summary prints only after a discharge, from its stored text.
          The unreachable handoff print branch was removed: HANDOFF opens the
          handoff page in a new tab. */}
      {printType === 'record' && createPortal(
        <InpatientRecordPrintView
          patient={patient}
          allergies={chartedAllergies}
          problems={Array.isArray(data.problems) ? data.problems : []}
          comorbidities={patientC}
          medications={currentMedications(medications)}
          latestVitals={Object.keys(latest).length ? latest : null}
          notes={signedNotes(notes, data.scribe_notes)}
          siteName={data.site_name}
        />,
        document.body
      )}
      {(printType === 'discharge' || printType === 'discharge-timeline') && dischargeSummary && createPortal(
        <DischargePrintView
          patient={patient}
          medications={currentMedications(medications).filter((m: any) => !m.unverified)}
          unverifiedMedicationCount={currentMedications(medications).filter((m: any) => m.unverified).length}
          summary={dischargeSummary.summary}
          summaryStatus={dischargeSummary.review_status === 'reviewed' ? 'reviewed' : 'draft'}
          reviewedByName={dischargeSummary.reviewed_by_name}
          reviewedAt={dischargeSummary.reviewed_at}
          dischargedAt={dischargeSummary.discharged_at}
          allergies={chartedAllergies}
          comorbidities={patientC}
          siteName={data.site_name}
          timeline={printType === 'discharge-timeline' ? dischargeSummary.timeline : undefined}
        />,
        document.body
      )}
      {printType === 'meds' && createPortal(
        <Suspense fallback={null}>
          <MedListPrintView
            patientName={patient.name}
            mrn={patient.mrn}
            bed={patient.bed}
            medications={(medications || []).map((m: any) => ({
              drug_name: m.drug_name,
              dosage: m.dosage,
              route: m.route,
              frequency: m.frequency,
              status: m.status,
              notes: m.notes,
              unverified: !!m.unverified,
            }))}
            allergies={chartedAllergies}
            siteName={data.site_name}
            onAfterPrint={() => setPrintType(null)}
          />
        </Suspense>,
        document.body
      )}
    </div>
  )
}


/* ─── Vital Card Sub-Component ─── */
function VitalCard({ label, value, unit, icon, warn }: { label: string; value: string; unit: string; icon: string; warn: boolean }) {
  return (
    <div className={`vital-card glass-card ${warn ? 'vital-card--warn' : ''}`}>
      <span className="vital-card__icon">{icon}</span>
      <span className="vital-card__value metric-mono">{value || '—'}</span>
      <span className="vital-card__unit">{unit}</span>
      <span className="vital-card__label">{label}</span>
    </div>
  )
}


/* ─── Risk Gauge ─── */
/**
 * Live presence chips — other clinicians currently viewing this patient.
 * Renders nothing when you're the only viewer, so it's silent in the common
 * single-viewer case and only surfaces genuine concurrent access (avoid two
 * people charting the same patient unknowingly).
 */
function PresenceChips({ viewers, selfId }: { viewers: { user_id: string; name: string; role: string }[]; selfId?: string }) {
  const others = viewers.filter(v => v.user_id !== selfId)
  if (others.length === 0) return null
  const initials = (name: string) => name.split(/\s+/).map(w => w[0]).slice(0, 2).join('').toUpperCase()
  return (
    <div className="command-bar__presence" role="status" aria-live="polite"
      style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 6 }}
      title={`Also viewing: ${others.map(o => `${o.name} (${o.role})`).join(', ')}`}>
      <span aria-hidden="true" style={{ display: 'inline-flex', width: 7, height: 7, borderRadius: '50%', background: 'var(--status-ok)', boxShadow: '0 0 0 3px color-mix(in srgb, var(--status-ok) 22%, transparent)' }} />
      <div style={{ display: 'flex' }}>
        {others.slice(0, 4).map((o, i) => (
          <span key={o.user_id} title={`${o.name} · ${o.role}`}
            style={{ display: 'inline-flex', alignItems: 'center', justifyContent: 'center', width: 22, height: 22, marginLeft: i ? -6 : 0, borderRadius: '50%', background: 'var(--bg-elevated)', border: '1.5px solid var(--border-glass)', fontSize: '0.6rem', fontWeight: 700, color: 'var(--text-secondary)' }}>
            {initials(o.name)}
          </span>
        ))}
      </div>
      <span style={{ fontSize: '0.68rem', color: 'var(--text-muted)' }}>
        {others.length === 1 ? `${others[0].name} also viewing` : `${others.length} others viewing`}
      </span>
    </div>
  )
}

/* REMOVED 2026-08-15: `News2Attribution`.
   A second, independently-computed "why this NEWS2" table. It is now
   components/vigil/News2Drivers.tsx — ONE renderer, fed either by the server's
   Tier 1 block (the alert's own decomposition, with the movement sentences that
   were being computed, stored, shipped and never read) or, when no alert draft
   exists, by this chart's live breakdown. Its good parts went with it: the
   points colouring, the single-parameter-3 rule, and the
   not-a-validated-model disclaimer. */

function RiskGauge({ score, level, rawNews2 }: { score: number; level: string; rawNews2?: number }) {
  const circumference = 2 * Math.PI * 40
  // The ring fills proportionally to the normalized index (a visual cue only);
  // the NUMBER shown is the raw NEWS2 aggregate, never a percentage — the 0-1
  // index is not a calibrated deterioration probability (see vigil_service).
  const dashOffset = circumference * (1 - Math.max(0, Math.min(1, score ?? 0)))
  const color = level === 'critical' ? 'var(--status-danger)' : level === 'elevated' ? 'var(--status-warn)' : 'var(--status-ok)'
  const hasNews2 = typeof rawNews2 === 'number'

  return (
    <div className="risk-gauge" title="NEWS2 aggregate score (RCP early-warning table). Not a validated probability.">
      <svg width="100%" height="100%" viewBox="0 0 100 100">
        <circle cx="50" cy="50" r="40" fill="none" stroke="var(--border-glass)" strokeWidth="6" />
        <circle
          cx="50" cy="50" r="40"
          fill="none" stroke={color} strokeWidth="6"
          strokeDasharray={circumference}
          strokeDashoffset={dashOffset}
          strokeLinecap="round"
          transform="rotate(-90 50 50)"
          style={{ transition: 'stroke-dashoffset 1s ease' }}
        />
      </svg>
      <div className="risk-gauge__label">
        <span className="risk-gauge__pct metric-mono" style={{ color, fontSize: hasNews2 ? '1.5rem' : undefined }}>{hasNews2 ? rawNews2 : '—'}</span>
        <span className="risk-gauge__level" style={{ fontSize: '0.55rem' }}>{hasNews2 ? 'NEWS2' : ''} · {level}</span>
      </div>
    </div>
  )
}



/* ═══════════════════════════════════════════════════════════════════════════
   REMOVED 2026-08-15: `AiNarrative` and its `GlassWindow` full-screen reader.

   WHAT THE WARD SAW: a panel headed "✦ MedGemma AI Assessment — Generating…"
   directly under the deterministic drivers, on EVERY chart, that NEVER
   resolved — held at "Generating…" for a full 150-second dedicated test and
   6+ minutes in the live pane. A spinner is a promise, and this one promised
   an assessment to a clinician standing at a deteriorating bedside.

   WHY IT COULD NOT RESOLVE, AND WHAT IT WAS COSTING: it opened a 512-token 27B
   stream on every chart open (GET /api/vigil/patients/{id}/analysis, cache
   commented out "for debugging"), took the same background LLM slot as Sage's
   alert advice at the same priority, and waited 120 s for it. So it was both
   the permanent shimmer AND a major cause of the 22% delivery rate on the
   advice tier it was competing with.

   WHY IT IS NOT SIMPLY FIXED: it duplicated the full four-section assessment
   that was deliberately relocated to handover/ward round, on a surface where
   Tier 1 — deterministic, 33-240 ms, no model — already answers "why is this
   score what it is, and which way is it going". Deleting a queue competitor was
   worth more than the panel. The endpoint went with it; see the note at
   server/routes/vigil_routes.py.

   The narrative text also fed `aiAssessment` / `hospitalCourse` on the two
   print views. Those only ever received it on COMPLETION, which never came, so
   both already fall back to their own standing wording.
   ═══════════════════════════════════════════════════════════════════════════ */
