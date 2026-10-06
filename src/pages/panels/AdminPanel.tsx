import './admin/Admin.css'
import { useState } from 'react'
import { rovingTabKeyDown } from '../../lib/tablist'
import AdminOverview from './admin/AdminOverview'
import AuditLogTable from './admin/AuditLogTable'
import CorrelationTrace from './admin/CorrelationTrace'
import IngestionMonitor from './admin/IngestionMonitor'
import SecurityActivity from './admin/SecurityActivity'
import AlertOperations from './admin/AlertOperations'
import ModelGovernance from './admin/ModelGovernance'
import ImpactDashboard from './admin/ImpactDashboard'
import ScorecardView from './admin/Scorecard'
import TrustCenter from './admin/TrustCenter'
import FlightRecorder from '../../components/FlightRecorder'
import UsersPanel from './admin/UsersPanel'
import DataPanel from './admin/DataPanel'
import RosterImport from './admin/RosterImport'
import ProvenanceReview from './admin/ProvenanceReview'
import { useClinicalSwitches } from '../../shellConfig'

type AdminTab = 'overview' | 'scorecard' | 'audit' | 'trace' | 'provenance' | 'ingestion' | 'security' | 'alerts' | 'models' | 'impact' | 'trust' | 'flight' | 'users' | 'roster' | 'data'

const ADMIN_TABS: { id: AdminTab; label: string; icon: React.ReactNode }[] = [
  {
    id: 'overview', label: 'Overview',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>,
  },
  {
    // The pilot's only evidence-producing surface — sits second, right after
    // Overview, because it is the thing the CIO and the champion open first.
    id: 'scorecard', label: 'Evaluation scorecard',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><line x1="6" y1="20" x2="6" y2="14"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="18" y1="20" x2="18" y2="10"/><line x1="3" y1="20" x2="21" y2="20"/></svg>,
  },
  {
    id: 'audit', label: 'Audit Logs',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>,
  },
  {
    id: 'trace', label: 'Trace',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>,
  },
  {
    // Sits with the forensic surfaces (Audit, Trace): "why did Sage say that?"
    // answered from evidence stored at the time of the turn, not re-read today.
    id: 'provenance', label: 'Sage Provenance',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><line x1="10" y1="12" x2="14" y2="12"/></svg>,
  },
  {
    id: 'ingestion', label: 'Ingestion',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>,
  },
  {
    id: 'security', label: 'Security',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>,
  },
  {
    id: 'alerts', label: 'Alerts',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>,
  },
  {
    id: 'models', label: 'Model Governance',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3"/><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>,
  },
  {
    id: 'impact', label: 'Impact',
    // Indian rupee, not a dollar sign (lucide indian-rupee).
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M6 3h12"/><path d="M6 8h12"/><path d="m6 13 8.5 8"/><path d="M6 13h3"/><path d="M9 13c6.667 0 6.667-10 0-10"/></svg>,
  },
  {
    id: 'trust', label: 'Trust Center',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="M9 12l2 2 4-4"/></svg>,
  },
  {
    id: 'flight', label: 'Flight Recorder',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>,
  },
  {
    id: 'users', label: 'Users',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>,
  },
  {
    // Sits next to Users: both answer "who is in this building today".
    id: 'roster', label: 'Roster Import',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>,
  },
  {
    id: 'data', label: 'Data & Backups',
    icon: <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>,
  },
]

/** The one region every sub-tab swaps. Stable so `aria-controls` never dangles. */
const ADMIN_PANEL_ID = 'admin-subpanel'

/** Under the Orb Ward Record profile (ORB_WARD_RECORD) Admin shows only what
 *  Ward Record sells, in this order: the Ward Record section in Data (ward
 *  intervals and the monthly rounds file), users, the audit log (and the trace
 *  it opens), the Flight Recorder and the Trust Center. */
const WARD_RECORD_ADMIN_TABS: readonly AdminTab[] = ['data', 'users', 'audit', 'trace', 'flight', 'trust']

export default function AdminPanel() {
  const { wardRecord } = useClinicalSwitches()
  const visibleTabs = wardRecord
    ? WARD_RECORD_ADMIN_TABS.flatMap(id => ADMIN_TABS.filter(t => t.id === id))
    : ADMIN_TABS
  const [chosenTab, setActiveTab] = useState<AdminTab>(wardRecord ? 'data' : 'overview')
  // A section the profile hides is never shown, even if it was open when the
  // switches arrived: the first section on the list opens instead.
  const activeTab: AdminTab = visibleTabs.some(t => t.id === chosenTab) ? chosenTab : visibleTabs[0].id
  const [traceCorrelationId, setTraceCorrelationId] = useState<string | null>(null)

  const handleTraceClick = (correlationId: string) => {
    setTraceCorrelationId(correlationId)
    setActiveTab('trace')
  }

  /* THE SUB-NAV IS A TAB LIST, SO IT SAYS SO.
     These 15 buttons carried no roles at all: a screen reader announced fifteen
     anonymous buttons, which one was open was conveyed by the pill fill alone,
     and every one of them held its own Tab stop — while the OUTER pillar rail,
     two levels up the same screen, already implements the full WAI-ARIA tabs
     pattern. Same pattern, same helper (lib/tablist), rather than a second
     dialect of the same idea living one directory away. */
  const handleTabKeyDown = rovingTabKeyDown(
    visibleTabs.map(t => t.id),
    activeTab,
    setActiveTab,
    id => `admin-tab-${id}`,
  )

  return (
    <div className="admin-panel">
      {/* ── Sub-navigation ── */}
      <nav
        className="admin-tabs"
        role="tablist"
        aria-label="Administration sections"
        aria-orientation="horizontal"
        onKeyDown={handleTabKeyDown}
      >
        {visibleTabs.map(tab => (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={activeTab === tab.id}
            aria-controls={ADMIN_PANEL_ID}
            tabIndex={activeTab === tab.id ? 0 : -1}
            className={`admin-tab ${activeTab === tab.id ? 'admin-tab--active' : ''}`}
            onClick={() => setActiveTab(tab.id)}
            id={`admin-tab-${tab.id}`}
          >
            {tab.icon}
            <span>{tab.label}</span>
          </button>
        ))}
      </nav>

      {/* ── Active Sub-panel ── */}
      <div
        className="admin-content"
        id={ADMIN_PANEL_ID}
        role="tabpanel"
        aria-labelledby={`admin-tab-${activeTab}`}
      >
        {activeTab === 'overview' && <AdminOverview />}
        {activeTab === 'scorecard' && <ScorecardView />}
        {activeTab === 'audit' && <AuditLogTable onTraceClick={handleTraceClick} />}
        {activeTab === 'trace' && <CorrelationTrace correlationId={traceCorrelationId} />}
        {activeTab === 'provenance' && <ProvenanceReview />}
        {activeTab === 'ingestion' && <IngestionMonitor />}
        {activeTab === 'security' && <SecurityActivity />}
        {activeTab === 'alerts' && <AlertOperations />}
        {activeTab === 'models' && <ModelGovernance />}
        {activeTab === 'impact' && <ImpactDashboard />}
        {activeTab === 'trust' && <TrustCenter />}
        {activeTab === 'flight' && <FlightRecorder />}
        {activeTab === 'users' && <UsersPanel />}
        {activeTab === 'roster' && <RosterImport />}
        {activeTab === 'data' && <DataPanel />}
      </div>
    </div>
  )
}
