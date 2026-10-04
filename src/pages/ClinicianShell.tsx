import { useState, useEffect, useCallback, useSyncExternalStore, lazy, Suspense, Component, type ReactNode } from 'react'
import { useTranslation } from 'react-i18next'
import { useAuth } from '../App'
import { getVitalsFeed } from '../api/client'
import { canSeeWardList } from '../lib/roles'
import AdmitPatientModal from '../components/AdmitPatientModal'
import OrbLogo from '../components/OrbLogo'
import PulseWidget from '../components/PulseWidget'
import ClockWidget from '../components/ClockWidget'
import AiStatusChip from '../components/AiStatusChip'
import { ActivePatientProvider } from '../contexts/ActivePatientProvider'
import { ActiveViewProvider } from '../contexts/ActiveViewProvider'
import { useActiveView } from '../contexts/ActiveViewContext'
import { SageRuntimeProvider } from '../contexts/SageRuntimeProvider'
import { PanelActiveContext, announceDataChanged } from '../contexts/PanelActiveContext'
import { gotoTab, onGotoTab, showShortcuts } from '../lib/orbBus'
import {
  DoctorHomePanel,
  getAccessibleTabs,
  getLandingTab,
  getModuleOffNotice,
  offersFloatingSage,
  getPrimaryAction,
  modulePanel,
  featurePanel,
  tabDef,
  currentFeatureFlags,
  currentHealthProbe,
  currentModuleFlags,
  refreshModuleFlags,
  subscribeModuleFlags,
  FEATURE_TAB_IDS,
  HOSPITAL_MODULE_IDS,
  type Tab,
} from '../shellConfig'
import ModuleOffNotice from '../components/ModuleOffNotice'
import SageFloatingAssistant from '../components/sage/SageFloatingAssistant'
import CommandPalette from '../components/CommandPalette'
import KeyboardShortcuts from '../components/KeyboardShortcuts'
import AlertPermissionButton from '../components/AlertPermissionButton'
import MfaEnrollmentModal from '../components/MfaEnrollmentModal'
import { initCriticalAlerts } from '../services/criticalAlerts'
import { rovingTabKeyDown } from '../lib/tablist'
import ThemeToggle from '../components/ThemeToggle'
import { useTheme } from '../lib/theme'
import './ClinicianShell.css'

/* How many mutations are waiting to sync. Module scope so the initial render
   and the polling effect read the queue exactly one way. */
const OFFLINE_QUEUE_KEY = 'orb_offline_queue'
function readOfflineQueueCount(): number {
  try {
    const raw = localStorage.getItem(OFFLINE_QUEUE_KEY)
    const queue = raw ? JSON.parse(raw) : []
    return Array.isArray(queue) ? queue.length : 0
  } catch {
    return 0
  }
}

/**
 * Per-panel error boundary so one lazy panel that throws during render (bad
 * payload, chunk-load failure) fails in place with a reload affordance instead
 * of blanking the entire shell. Panels are mounted in hidden divs, so even an
 * inactive background panel's bad data would otherwise take down the whole app.
 */
/**
 * A failed lazy import after a deploy means this tab's index.html references
 * hashed chunks that no longer exist (npm run build replaced dist/, and the
 * autoUpdate service worker's skipWaiting + cleanupOutdatedCaches purged the
 * old precache). Matches Chrome/Edge, Firefox, and Safari phrasings plus
 * Vite's CSS preload helper.
 */
const STALE_CHUNK_RE = /Failed to fetch dynamically imported module|Unable to preload CSS|Importing a module script failed|error loading dynamically imported module/i
const CHUNK_RELOAD_FLAG = 'orb:chunk-reload-at'
// Reload loops re-fail within seconds; distinct deploys are far apart. A
// timestamp window (instead of a one-shot boolean) lets a later deploy in the
// same tab session auto-recover too.
const CHUNK_RELOAD_WINDOW_MS = 30_000

function isStaleChunkError(error: unknown): boolean {
  return error instanceof Error && STALE_CHUNK_RE.test(error.message)
}

function chunkReloadAllowed(): boolean {
  const last = Number(sessionStorage.getItem(CHUNK_RELOAD_FLAG))
  return !(last > 0 && Date.now() - last < CHUNK_RELOAD_WINDOW_MS)
}

class PanelErrorBoundary extends Component<{ label: string; active: boolean; children: ReactNode }, { hasError: boolean; updating: boolean }> {
  state = { hasError: false, updating: false }
  static getDerivedStateFromError(error: unknown) {
    // Stale chunk + no recent auto-reload: show the quiet "updating" note
    // while componentDidCatch triggers the one-shot page reload, so the
    // deploy refresh reads as a routine load, not a failure.
    return { hasError: true, updating: isStaleChunkError(error) && chunkReloadAllowed() }
  }
  componentDidCatch(error: unknown) {
    console.error('[Orb] Panel error:', this.props.label, error)
    // Only a fresh index.html can map to the new asset hashes, so reload the
    // page once. The sessionStorage timestamp stops a reload loop if the new
    // page still can't load its chunks (real outage): the second failure
    // inside the window falls through to the normal error UI below.
    if (isStaleChunkError(error) && chunkReloadAllowed()) {
      sessionStorage.setItem(CHUNK_RELOAD_FLAG, String(Date.now()))
      window.location.reload()
    }
  }
  reset = () => this.setState({ hasError: false, updating: false })
  render() {
    if (this.state.updating) {
      // One-shot reload is in flight — mirror the Suspense fallback styling.
      return <div className="orb-panel-loading">Updating to the latest version...</div>
    }
    if (this.state.hasError) {
      return (
        <div className="orb-panel-error" role="alert" style={{ padding: '2rem', textAlign: 'center' }}>
          <p>This panel hit an error and couldn't be shown.</p>
          <button onClick={this.reset} style={{ padding: '0.5rem 1.2rem', cursor: 'pointer' }}>
            Reload panel
          </button>
        </div>
      )
    }
    // Hidden panels stay mounted; this tells their pollers to stand down.
    return <PanelActiveContext.Provider value={this.props.active}>{this.props.children}</PanelActiveContext.Provider>
  }
}

const NursePanel = lazy(() => import('./panels/nurse/NursePanel'))
const RoundPanel = lazy(() => import('./panels/RoundPanel'))
const HelixPanel = lazy(() => import('./panels/HelixPanel'))
const OrdersPanel = lazy(() => import('../components/orders/OrdersPanel'))
const PharmacyVerifyQueue = lazy(() => import('../components/helix/PharmacyVerifyQueue'))
const CommandCenter = lazy(() => import('../components/command/CommandCenter'))
const CapacitySimulator = lazy(() => import('../components/capacity/CapacitySimulator'))
const RevenueIntegrityPanel = lazy(() => import('../components/coding/RevenueIntegrityPanel'))
const VigilPanel = lazy(() => import('./panels/VigilPanel'))
const RelayPanel = lazy(() => import('./panels/RelayPanel'))
const SagePanel = lazy(() => import('./panels/SagePanel'))
const ScribePanel = lazy(() => import('./panels/ScribePanel'))
const LensPanel = lazy(() => import('./panels/LensPanel'))
const ForecastPanel = lazy(() => import('./panels/ForecastPanel'))
const AppointmentsPanel = lazy(() => import('./panels/AppointmentsPanel'))
const SurgicalSuitePanel = lazy(() => import('./panels/SurgicalSuitePanel'))
const AdminPanel = lazy(() => import('./panels/AdminPanel'))

/** Publishes the shell's active tab into ActiveViewContext so every Sage
 *  surface knows what screen the clinician is on, and clears any stale panel
 *  selection when the tab changes. */
function ViewTabSync({ activeTab }: { activeTab: string }) {
  const { setViewTab, setViewSelection } = useActiveView()
  useEffect(() => {
    setViewTab(activeTab)
    setViewSelection(null)
  }, [activeTab, setViewTab, setViewSelection])
  return null
}

export default function ClinicianShell() {
  const { user, logout } = useAuth()
  const { t, i18n } = useTranslation()
  /* The rail is derived from the module and feature flags, which /api/health
     settles once per sign-in. Subscribing here is what lets an answer that
     arrives after first paint add or remove a tab; the three snapshots are the
     only inputs the derivation below reads from the store. A probe that fails
     or answers non-OK leaves every opt-in module and every feature OFF, and
     every other module at its default ON, so a default site's rail is the same
     before and after it answers. */
  useSyncExternalStore(subscribeModuleFlags, currentModuleFlags)
  useSyncExternalStore(subscribeModuleFlags, currentFeatureFlags)
  useSyncExternalStore(subscribeModuleFlags, currentHealthProbe)
  useEffect(() => { void refreshModuleFlags() }, [])
  /* One role input, one answer: everything role-dependent in this component
     comes through shellConfig. `user.role` must not appear in the JSX below —
     that rule is what stops the flag drift this file was refactored out of
     (DUAL_EXPERIENCE_DESIGN.md §3). */
  const accessibleTabs = getAccessibleTabs(user?.role, user?.department)
  // Rail order is the config's order, so the declared product order and the
  // rendered order cannot disagree.
  const visibleTabs = accessibleTabs.flatMap(id => {
    const def = tabDef(id)
    return def ? [def] : []
  })
  // The landing tab is named by the config (and overridable per deployment),
  // not implied by "whatever happens to be first". It always resolves to a tab
  // this role can reach, so the shell never initializes activeTab to undefined.
  const landingTab = getLandingTab(user?.role, user?.department)
  const [activeTab, setActiveTabState] = useState<Tab>(landingTab)
  // Mount-on-first-view: a panel's subtree is only rendered once its tab has
  // been visited. After that it stays mounted (hidden via display:none) so its
  // state survives switching away, but unseen panels never fetch/poll at login.
  const [visitedTabs, setVisitedTabs] = useState<Set<Tab>>(() => new Set([landingTab]))

  /* The visit is recorded HERE — in the one function every navigation source
     already funnels through — rather than in an effect keyed on activeTab.
     The effect version mounted the panel a render LATE: the render that
     switched tabs still held the previous visited set, so `shouldRenderPanel`
     answered no, and the panel appeared only on the extra render the effect
     then triggered. On a first visit that is a blank frame where the panel
     should be. Both setState calls batch, so this is one render, and the
     panel mounts in the same one that moves the tab. */
  const setActiveTab = useCallback((tab: Tab) => {
    setVisitedTabs(prev => (prev.has(tab) ? prev : new Set(prev).add(tab)))
    setActiveTabState(tab)
  }, [])
  /* When the rail changes under the open tab (the health probe answered after
     first paint and switched an opt-in module on or a module off), and the open
     tab is no longer on it, move to the landing tab. Adjusted during render,
     keyed on the rail itself, so a rail that never changes (every default site)
     never takes this branch. A ward clerk who signed in before the probe
     answered lands on Beds the moment it does, instead of on a rail with the
     notice gone and nothing open. */
  const railKey = accessibleTabs.join(',')
  const [seenRailKey, setSeenRailKey] = useState(railKey)
  if (seenRailKey !== railKey) {
    setSeenRailKey(railKey)
    if (!accessibleTabs.includes(activeTab) && accessibleTabs.includes(landingTab)) {
      setVisitedTabs(prev => (prev.has(landingTab) ? prev : new Set(prev).add(landingTab)))
      setActiveTabState(landingTab)
    }
  }
  /* A HIS role (receptionist, ward clerk, lab) whose feature is off has an
     EMPTY rail. This names why, instead of showing a blank body. Null for every
     other role. */
  const moduleOffNotice = getModuleOffNotice(user?.role, user?.department)
  // While the notice stands the header names the role's own home (Beds, Front
  // Desk, Laboratory), never the fallback tab the account cannot open.
  const headerTab: Tab = moduleOffNotice ? moduleOffNotice.home : activeTab
  // The HIS roles hold no sage.query, so their screen carries no Sage orb.
  // Every other role keeps it exactly as before.
  const floatingSage = offersFloatingSage(user?.role)
  const shouldRenderPanel = (tab: Tab) => accessibleTabs.includes(tab) && visitedTabs.has(tab)
  // The rail's one call-to-action, or null. Permission-derived — see shellConfig.
  const primaryAction = getPrimaryAction(user?.role, user?.department)
  const [isAdmittingPatient, setIsAdmittingPatient] = useState(false)
  const [isEnrollingMfa, setIsEnrollingMfa] = useState(false)

  // Command palette dispatches "orb:goto-tab" then "orb:open-patient" when the
  // user jumps to a patient from anywhere. Honor goto-tab; and because the
  // open-patient event is dispatched synchronously — before VigilPanel has
  // mounted on the very first jump — re-dispatch it on the next tick so the
  // freshly-mounted VigilPanel listener catches it. A re-entrancy guard stops
  // the replay from looping (VigilPanel treats a repeat as an idempotent
  // re-select).
  useEffect(() => {
    const handleGotoTab = (e: Event) => {
      const tab = (e as CustomEvent).detail?.tab as Tab | undefined
      if (tab && accessibleTabs.includes(tab)) setActiveTab(tab)
    }
    let replaying = false
    const handleOpenPatient = (e: Event) => {
      if (replaying) { replaying = false; return }
      const patientId = (e as CustomEvent).detail?.patientId
      if (!patientId) return
      setTimeout(() => {
        replaying = true
        window.dispatchEvent(new CustomEvent('orb:open-patient', { detail: { patientId } }))
      }, 200)
    }
    window.addEventListener('orb:goto-tab', handleGotoTab)
    window.addEventListener('orb:open-patient', handleOpenPatient)
    return () => {
      window.removeEventListener('orb:goto-tab', handleGotoTab)
      window.removeEventListener('orb:open-patient', handleOpenPatient)
    }
  }, [accessibleTabs, setActiveTab])

  // Wire browser notifications + chime + title-flash for critical realtime
  // events while the tab is backgrounded. Teardown on unmount / logout.
  useEffect(() => initCriticalAlerts(), [])

  // HONESTY LABEL FOR THE WHOLE SHELL. While the simulator is writing vital
  // signs, every screen says so, not only the chart that happens to be open.
  // Advisory: if the question cannot be asked, no label is shown, and the
  // per-row labels on the chart and the board still stand.
  const [vitalsSimulated, setVitalsSimulated] = useState(false)
  useEffect(() => {
    let cancelled = false
    try {
      // Only a role that sees vital signs is asked (the route wants vitals.read).
      if (canSeeWardList()) {
        getVitalsFeed()
          .then(feed => { if (!cancelled) setVitalsSimulated(!!feed?.simulated) })
          .catch(() => {})
      }
    } catch {
      /* no feed information: no label */
    }
    return () => { cancelled = true }
  }, [])

  // Theme cycle, storage keys and the repaint-not-animate swap all live in
  // lib/theme so the pre-login screens carry the identical switch.
  const { theme, toggleTheme } = useTheme()

  // 'surgeon' is a doctor variant the backend may emit on a direct/refresh path
  // (see getAccessibleTabs); label it Physician rather than the generic "Staff".
  const normalizedRole = (user?.role as string) === 'surgeon' ? 'doctor' : (user?.role ?? '')
  // Every role server/roles.py stores, so each reads by its own name in the
  // user chip; anything else is "Staff".
  const roleKey = [
    'doctor', 'nurse', 'admin', 'pharmacist', 'cashier',
    'receptionist', 'ward_clerk', 'lab_technician', 'pathologist', 'phlebotomist',
  ].includes(normalizedRole)
    ? normalizedRole
    : 'staff'
  const roleLabel = t(`role.${roleKey}`)
  // A hospital-wide account has no department: the chip read "Administrator ·"
  // and its tooltip "Administrator · null".
  const roleLine = [roleLabel, user?.department].filter(Boolean).join(' · ')

  const toggleLanguage = () => {
    i18n.changeLanguage(i18n.language === 'hi' ? 'en' : 'hi')
  }

  // Roving-focus keyboard navigation for the nav tablist (Arrow/Home/End),
  // with automatic activation so screen-reader/keyboard users move between
  // pillars the same way a pointer user would.
  /* Extracted to lib/tablist so NursePanel's shift strip — which declared the
     ARIA roles but never implemented the keys — uses the identical behaviour. */
  const handleNavKeyDown = rovingTabKeyDown(
    visibleTabs.map(tab => tab.id),
    activeTab,
    setActiveTab,
    id => `nav-${id}`,
  )

  // Navigation bus: any surface (Sage, deep links) can drive the shell via
  // orb:goto-tab. Only tabs this role can access are honored.
  useEffect(() => {
    return onGotoTab(tab => {
      if (accessibleTabs.includes(tab as Tab)) setActiveTab(tab as Tab)
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accessibleTabs.join(',')])

  /* Keyboard chords go through the SAME bus every other navigation source uses
     (the command palette, Sage, deep links) rather than calling setActiveTab
     directly — so the RBAC filter above is the one place that decides whether a
     tab may be opened, and a shortcut cannot become a second door into it.
     Stable identity so the key listener is not rebound on every shell render. */
  const handleShortcutGoto = useCallback((tab: string) => { gotoTab(tab) }, [])

  const [isOffline, setIsOffline] = useState(!navigator.onLine)
  // Pending count = requests client.ts has queued to localStorage while offline
  // (key 'orb_offline_queue'), plus a brief "Synced" confirmation when it drains.
  // Seeded from the queue itself, not 0. Reading it in the initializer means
  // the very first paint already shows a real backlog; the old effect-seeded
  // version rendered "0 pending" once and corrected itself a render later,
  // which on a tablet resuming offline work is a visible flicker of "nothing
  // to sync" over a queue that has items in it.
  const [pendingCount, setPendingCount] = useState(readOfflineQueueCount)
  const [justSynced, setJustSynced] = useState(false)
  // Count of queued mutations that failed permanently on replay (a 4xx that will
  // never succeed — e.g. the order went inactive before the offline chart synced).
  // client.ts drops these from the queue and dispatches 'orb:offline-sync-failed'
  // so we can warn the clinician the action did NOT go through (it was shown as
  // saved offline); dismissible because it needs a human to re-do the action.
  const [syncFailed, setSyncFailed] = useState(0)

  useEffect(() => {
    let syncedTimer: ReturnType<typeof setTimeout> | undefined
    // Same read the initializer used, so the effect starts from what is already
    // on screen rather than re-announcing it.
    let prevCount = readOfflineQueueCount()

    const refresh = () => {
      const next = readOfflineQueueCount()
      // Queue drained (had items, now empty) → briefly confirm a successful sync.
      if (prevCount > 0 && next === 0) {
        setJustSynced(true)
        if (syncedTimer) clearTimeout(syncedTimer)
        syncedTimer = setTimeout(() => setJustSynced(false), 3000)
      }
      if (next > 0) setJustSynced(false)
      prevCount = next
      setPendingCount(next)
    }

    const handleOnline = () => { setIsOffline(false); refresh() }
    const handleOffline = () => { setIsOffline(true); setJustSynced(false); refresh() }
    // A queued mutation was dropped after a permanent replay failure — surface it.
    const handleSyncFailed = (e: Event) => {
      const failed = (e as CustomEvent).detail?.failed
      setSyncFailed((prev) => prev + (Array.isArray(failed) ? failed.length : 1))
      setJustSynced(false)
    }

    window.addEventListener('online', handleOnline)
    window.addEventListener('offline', handleOffline)
    window.addEventListener('orb:offline-sync-failed', handleSyncFailed)
    // The 'storage' event only fires for OTHER tabs, and client.ts mutates the
    // queue synchronously in THIS tab, so poll to observe same-tab changes.
    const poll = setInterval(refresh, 2000)

    return () => {
      window.removeEventListener('online', handleOnline)
      window.removeEventListener('offline', handleOffline)
      window.removeEventListener('orb:offline-sync-failed', handleSyncFailed)
      clearInterval(poll)
      if (syncedTimer) clearTimeout(syncedTimer)
    }
  }, [])

  return (
    <ActivePatientProvider>
    <ActiveViewProvider>
    <SageRuntimeProvider>
    <ViewTabSync activeTab={activeTab} />
    <div className="shell">
      <aside className="shell__sidebar glass">
        <div className="shell__sidebar-top">
          <div className="shell__logo"><OrbLogo size={32} /></div>
          <nav
            className="shell__nav"
            role="tablist"
            aria-label="Orb pillars"
            aria-orientation="vertical"
            onKeyDown={handleNavKeyDown}
          >
            {visibleTabs.map(tab => (
              <button
                key={tab.id}
                role="tab"
                aria-selected={activeTab === tab.id}
                aria-controls={`panel-${tab.id}`}
                tabIndex={activeTab === tab.id ? 0 : -1}
                className={`shell__nav-item ${activeTab === tab.id ? 'shell__nav-item--active' : ''}`}
                onClick={() => setActiveTab(tab.id)}
                id={`nav-${tab.id}`}
                aria-label={t(`nav.${tab.id}`)}
                title={`${t(`nav.${tab.id}`)} · Orb ${tab.pillar}`}
              >
                {tab.icon}
                <span className="shell__nav-label">{t(`nav.${tab.id}`)}</span>
              </button>
            ))}
          </nav>
        </div>

        <div className="shell__sidebar-bottom">
          {/* Mirrors POST /api/admissions/admit, which requires `patient.write`
              (server/routes/admission_routes.py). The old gate was "anyone but
              a pharmacist", so nurses and admins got the primary CTA on every
              screen and a 403 the moment they filled the form in — a button
              that existed only to fail. Resolved through shellConfig, which
              ANDs the experience's placement with the permission mirror, so
              adding a role cannot silently re-open it. */}
          {primaryAction === 'admit' && (
            <button className="shell__admit-patient" id="btn-admit-patient" title={t('shell.admitPatient')} aria-label={t('shell.admitPatient')} onClick={() => setIsAdmittingPatient(true)}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <line x1="12" y1="5" x2="12" y2="19"/>
                <line x1="5" y1="12" x2="19" y2="12"/>
              </svg>
              <span>{t('shell.admitPatient')}</span>
            </button>
          )}
          <div className="shell__user-card">
            <div className="shell__user-avatar">{user?.name?.charAt(0)}</div>
            <div className="shell__user-info">
              {/* The chip ellipsizes at sidebar width, so the full name/role
                  must stay recoverable on hover (DEMO_READINESS §2.7). */}
              <span className="shell__user-name" title={user?.name}>{user?.name}</span>
              <span className="shell__user-role" title={roleLine}>{roleLine}</span>
            </div>
            <button
              className="shell__user-security"
              onClick={() => setIsEnrollingMfa(true)}
              id="btn-mfa-enroll"
              title={t('shell.security')}
              aria-label={t('shell.security')}
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>
              </svg>
            </button>
          </div>
          <button className="shell__logout" onClick={logout} id="btn-logout" title={t('shell.signOutTitle')} aria-label={t('shell.signOutTitle')}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
              <polyline points="16 17 21 12 16 7"/>
              <line x1="21" y1="12" x2="9" y2="12"/>
            </svg>
            <span>{t('shell.signOut')}</span>
          </button>
        </div>
      </aside>

      <main className="shell__main">
        <header className="shell__header">
          <div className="shell__header-left">
            <h1 className="shell__page-title">
              Orb {tabDef(headerTab)?.pillar}
            </h1>
            <span className="shell__page-subtitle">{t(`subtitle.${headerTab}`)}</span>
          </div>
          <div className="shell__header-right">
            {syncFailed > 0 ? (
              <div className="shell__offline-badge shell__offline-badge--failed" role="alert" aria-live="assertive">
                <span className="shell__offline-dot" />
                {t('status.syncFailed', { count: syncFailed })}
                <button
                  className="shell__offline-dismiss"
                  onClick={() => setSyncFailed(0)}
                  title={t('status.syncFailedDismiss')}
                  aria-label={t('status.syncFailedDismiss')}
                >
                  ✕
                </button>
              </div>
            ) : isOffline ? (
              <div className="shell__offline-badge" role="status" aria-live="polite">
                <span className="shell__offline-dot" />
                {pendingCount > 0
                  ? t('status.offlinePending', { count: pendingCount })
                  : t('status.offlineCaching')}
              </div>
            ) : pendingCount > 0 ? (
              <div className="shell__offline-badge shell__offline-badge--syncing" role="status" aria-live="polite">
                <span className="shell__offline-dot" />
                {t('status.syncingPending', { count: pendingCount })}
              </div>
            ) : justSynced ? (
              <div className="shell__offline-badge shell__offline-badge--synced" role="status" aria-live="polite">
                <span className="shell__synced-check" aria-hidden="true">✓</span>
                {t('status.synced')}
              </div>
            ) : null}
            {vitalsSimulated && (
              <div
                className="shell__offline-badge"
                role="note"
                title="Vital signs on this system are generated by the simulator. They are not readings from patients."
              >
                Simulated vitals
              </div>
            )}
            <ClockWidget />
            <AiStatusChip />
            <PulseWidget />
            <AlertPermissionButton />
            {/* Keyboard reference. Same 34px icon recipe as its neighbours
                (.shell__icon-toggle) so the header stays one row of identical
                controls rather than a collection. */}
            <button
              type="button"
              className="shell__icon-toggle"
              onClick={showShortcuts}
              title="Keyboard shortcuts (?)"
              aria-label="Keyboard shortcuts"
              id="btn-shortcuts"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <rect x="2" y="6" width="20" height="12" rx="2"/>
                <path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M6 14h.01M18 14h.01"/>
                <path d="M9.5 14h5"/>
              </svg>
            </button>
            <button
              className="shell__lang-toggle"
              onClick={toggleLanguage}
              title={t('shell.switchLanguage')}
              aria-label={t('shell.switchLanguage')}
              id="btn-lang-toggle"
            >
              {i18n.language === 'hi' ? 'EN' : 'हि'}
            </button>
            <ThemeToggle theme={theme} onToggle={toggleTheme} />
            </div>
        </header>

        <section className="shell__body">
          <Suspense fallback={<div className="orb-panel-loading">Loading panel...</div>}>
          {/* The doctor home. `DoctorHomePanel` is null until its module is in
              the build, and shellConfig drops 'today' from every tab list in
              that case, so this branch cannot render an empty panel: a tab with
              no panel is not a tab. */}
          {DoctorHomePanel && shouldRenderPanel('today') && (
            <div id={`panel-today`} role="tabpanel" aria-labelledby={`nav-today`} style={{ display: activeTab === 'today' ? 'contents' : 'none' }}>
              <PanelErrorBoundary label="today" active={activeTab === 'today'}><DoctorHomePanel /></PanelErrorBoundary>
            </div>
          )}
          {shouldRenderPanel('shift') && (
            <div id={`panel-shift`} role="tabpanel" aria-labelledby={`nav-shift`} style={{ display: activeTab === 'shift' ? 'contents' : 'none' }}>
              <PanelErrorBoundary label="shift" active={activeTab === 'shift'}><NursePanel /></PanelErrorBoundary>
            </div>
          )}
          {shouldRenderPanel('helix') && (
            <div id={`panel-helix`} role="tabpanel" aria-labelledby={`nav-helix`} style={{ display: activeTab === 'helix' ? 'contents' : 'none' }}>
              <PanelErrorBoundary label="helix" active={activeTab === 'helix'}><HelixPanel /></PanelErrorBoundary>
            </div>
          )}
          {shouldRenderPanel('pharmacy-verify') && (
            <div id={`panel-pharmacy-verify`} role="tabpanel" aria-labelledby={`nav-pharmacy-verify`} style={{ display: activeTab === 'pharmacy-verify' ? 'contents' : 'none' }}>
              <PanelErrorBoundary label="pharmacy-verify" active={activeTab === 'pharmacy-verify'}><PharmacyVerifyQueue /></PanelErrorBoundary>
            </div>
          )}
          {shouldRenderPanel('vigil') && (
            <div id={`panel-vigil`} role="tabpanel" aria-labelledby={`nav-vigil`} style={{ display: activeTab === 'vigil' ? 'contents' : 'none' }}>
              <PanelErrorBoundary label="vigil" active={activeTab === 'vigil'}><VigilPanel role={user?.role} /></PanelErrorBoundary>
            </div>
          )}
          {shouldRenderPanel('round') && (
            <div id={`panel-round`} role="tabpanel" aria-labelledby={`nav-round`} style={{ display: activeTab === 'round' ? 'contents' : 'none' }}>
              <PanelErrorBoundary label="round" active={activeTab === 'round'}><RoundPanel /></PanelErrorBoundary>
            </div>
          )}
          {shouldRenderPanel('orders') && (
            <div id={`panel-orders`} role="tabpanel" aria-labelledby={`nav-orders`} style={{ display: activeTab === 'orders' ? 'contents' : 'none' }}>
              <PanelErrorBoundary label="orders" active={activeTab === 'orders'}><OrdersPanel /></PanelErrorBoundary>
            </div>
          )}
          {shouldRenderPanel('command') && (
            <div id={`panel-command`} role="tabpanel" aria-labelledby={`nav-command`} style={{ display: activeTab === 'command' ? 'contents' : 'none' }}>
              <PanelErrorBoundary label="command" active={activeTab === 'command'}><CommandCenter /></PanelErrorBoundary>
            </div>
          )}
          {shouldRenderPanel('capacity') && (
            <div id={`panel-capacity`} role="tabpanel" aria-labelledby={`nav-capacity`} style={{ display: activeTab === 'capacity' ? 'contents' : 'none' }}>
              <PanelErrorBoundary label="capacity" active={activeTab === 'capacity'}><CapacitySimulator /></PanelErrorBoundary>
            </div>
          )}
          {shouldRenderPanel('coding') && (
            <div id={`panel-coding`} role="tabpanel" aria-labelledby={`nav-coding`} style={{ display: activeTab === 'coding' ? 'contents' : 'none' }}>
              <PanelErrorBoundary label="coding" active={activeTab === 'coding'}><RevenueIntegrityPanel /></PanelErrorBoundary>
            </div>
          )}
          {shouldRenderPanel('relay') && (
            <div id={`panel-relay`} role="tabpanel" aria-labelledby={`nav-relay`} style={{ display: activeTab === 'relay' ? 'contents' : 'none' }}>
              <PanelErrorBoundary label="relay" active={activeTab === 'relay'}><RelayPanel /></PanelErrorBoundary>
            </div>
          )}
          {shouldRenderPanel('sage') && (
            <div id={`panel-sage`} role="tabpanel" aria-labelledby={`nav-sage`} style={{ display: activeTab === 'sage' ? 'contents' : 'none' }}>
              <PanelErrorBoundary label="sage" active={activeTab === 'sage'}><SagePanel userName={user?.name} /></PanelErrorBoundary>
            </div>
          )}
          {shouldRenderPanel('scribe') && (
            <div id={`panel-scribe`} role="tabpanel" aria-labelledby={`nav-scribe`} style={{ display: activeTab === 'scribe' ? 'contents' : 'none' }}>
              <PanelErrorBoundary label="scribe" active={activeTab === 'scribe'}><ScribePanel /></PanelErrorBoundary>
            </div>
          )}
          {shouldRenderPanel('lens') && (
            <div id={`panel-lens`} role="tabpanel" aria-labelledby={`nav-lens`} style={{ display: activeTab === 'lens' ? 'contents' : 'none' }}>
              <PanelErrorBoundary label="lens" active={activeTab === 'lens'}><LensPanel /></PanelErrorBoundary>
            </div>
          )}
          {shouldRenderPanel('forecast') && (
            <div id={`panel-forecast`} role="tabpanel" aria-labelledby={`nav-forecast`} style={{ display: activeTab === 'forecast' ? 'contents' : 'none' }}>
              <PanelErrorBoundary label="forecast" active={activeTab === 'forecast'}><ForecastPanel /></PanelErrorBoundary>
            </div>
          )}
          {shouldRenderPanel('appointments') && (
            <div id={`panel-appointments`} role="tabpanel" aria-labelledby={`nav-appointments`} style={{ display: activeTab === 'appointments' ? 'contents' : 'none' }}>
              <PanelErrorBoundary label="appointments" active={activeTab === 'appointments'}><AppointmentsPanel /></PanelErrorBoundary>
            </div>
          )}
          {shouldRenderPanel('surgical-suite') && (
            <div id={`panel-surgical-suite`} role="tabpanel" aria-labelledby={`nav-surgical-suite`} style={{ display: activeTab === 'surgical-suite' ? 'contents' : 'none' }}>
              <PanelErrorBoundary label="surgical-suite" active={activeTab === 'surgical-suite'}><SurgicalSuitePanel /></PanelErrorBoundary>
            </div>
          )}
          {shouldRenderPanel('admin') && (
            <div id={`panel-admin`} role="tabpanel" aria-labelledby={`nav-admin`} style={{ display: activeTab === 'admin' ? 'contents' : 'none' }}>
              <PanelErrorBoundary label="admin" active={activeTab === 'admin'}><AdminPanel /></PanelErrorBoundary>
            </div>
          )}
          {/* The hospital-administration modules. Every branch above names ONE
              panel because each takes different props; these eleven take none,
              so they mount through shellConfig's `modulePanel` lookup and the
              shell never names a module panel (shellConfig.tsx §"A tab with no
              panel is not a tab").

              THIS LOOP IS THE HALF THAT WAS MISSING. shellConfig registered the
              tabs — flag on, panel file present — and the rail rendered them,
              but nothing here mounted them, so all eleven tabs opened on an
              empty <section>: Billing, Payments, Payer, Procurement, Front Desk,
              Housekeeping, Workforce, Diet & Kitchen, Biomedical, NABH and ABDM.
              Every panel's own unit tests passed and shellConfig's tab-derivation
              tests passed; nothing asserted that the shell mounts what the rail
              advertises. Deriving the list from HOSPITAL_MODULE_IDS rather than
              writing eleven branches is what keeps the twelfth module from
              landing in the same hole.

              THE WRAPPER IS THE SCROLLER. With display:contents the module root
              became a direct child of .shell__body, which clips, and no module
              root scrolls itself, so everything below the first screen (the
              NABH register, the report form's submit, the diet lists) could not
              be reached. One scroll container here covers every module, present
              and future, and reserves the floating Sage orb's corner. */}
          {HOSPITAL_MODULE_IDS.map(id => {
            const Panel = modulePanel(id)
            if (!Panel || !shouldRenderPanel(id)) return null
            return (
              <div
                key={id}
                id={`panel-${id}`}
                role="tabpanel"
                aria-labelledby={`nav-${id}`}
                className="shell__module-scroll"
                style={{ display: activeTab === id ? 'flex' : 'none' }}
              >
                <PanelErrorBoundary label={id} active={activeTab === id}><Panel /></PanelErrorBoundary>
              </div>
            )
          })}
          {/* Feature tabs (OPD): the same mount-through-a-lookup as the modules,
              through shellConfig's `featurePanel`, so a feature panel that lands
              is mounted the day its tab appears. */}
          {FEATURE_TAB_IDS.map(id => {
            const Panel = featurePanel(id)
            if (!Panel || !shouldRenderPanel(id)) return null
            return (
              <div
                key={id}
                id={`panel-${id}`}
                role="tabpanel"
                aria-labelledby={`nav-${id}`}
                className="shell__module-scroll"
                style={{ display: activeTab === id ? 'flex' : 'none' }}
              >
                <PanelErrorBoundary label={id} active={activeTab === id}><Panel /></PanelErrorBoundary>
              </div>
            )
          })}
          {/* A HIS role whose feature is switched off: say so plainly, never a
              blank body and never tabs the account cannot open. */}
          {moduleOffNotice && (
            <ModuleOffNotice subject={moduleOffNotice.subject} state={moduleOffNotice.state} />
          )}
                  </Suspense>
        </section>
      </main>

      {isAdmittingPatient && (
        <AdmitPatientModal
          onClose={() => setIsAdmittingPatient(false)}
          onSuccess={() => {
            setIsAdmittingPatient(false)
            // A full reload threw away every half-written note and order on
            // the other panels. Pollers and patient lists (useDataChanged)
            // refresh now; hidden panels when they are next opened.
            announceDataChanged()
          }}
        />
      )}

      {isEnrollingMfa && <MfaEnrollmentModal onClose={() => setIsEnrollingMfa(false)} />}

      {/* Global Cmd/Ctrl-K patient search — registers its own key listener. */}
      <CommandPalette />

      {/* Global keyboard layer: `g` + letter to move between the surfaces a
          clinician opens fifty times a shift, `?` for the reference card. Given
          the SAME accessibleTabs the nav rail is built from, so a shortcut can
          never reach a tab this role has no permission for. */}
      <KeyboardShortcuts accessibleTabs={accessibleTabs} onGoto={handleShortcutGoto} />

      {floatingSage && <SageFloatingAssistant activeTab={activeTab} />}
    </div>
    </SageRuntimeProvider>
    </ActiveViewProvider>
    </ActivePatientProvider>
  )
}


