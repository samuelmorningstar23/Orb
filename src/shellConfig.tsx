import { lazy, type ComponentType, type LazyExoticComponent, type ReactNode } from 'react'
import { hasPermission, PATIENT_WRITE, type Permission } from './lib/roles'

/**
 * THE SHELL'S ONE ROLE INPUT.
 *
 * ClinicianShell used to answer "what does this role see?" in four different
 * places: a switch returning a tab array, the implicit rule that the landing
 * tab is `accessibleTabs[0]`, an inline `role !== 'pharmacist'` gate on the
 * primary CTA, and the master TABS list's own ordering. Four sources of truth
 * about one question is how the admit-CTA defect was born — the CTA's role list
 * drifted from the server's permission map and every nurse and administrator
 * got a button that existed only to 403 (DUAL_EXPERIENCE_DESIGN.md §1.2.2).
 *
 * So: one declarative table. A role's experience is `home` + ordered, grouped
 * `tabs` + a `primaryAction`, and `getAccessibleTabs` is DERIVED from it rather
 * than written twice. The review rule that keeps it that way is in the design
 * doc §3: `user.role` may not appear in ClinicianShell's JSX outside a lookup
 * through this file.
 *
 * What this file is deliberately NOT: a panel registry. Panels stay mounted by
 * name in the shell because they take different props (VigilPanel needs the
 * role, SagePanel the user name) and hiding that behind a generic renderer buys
 * nothing but indirection. The one exception is the doctor home, below, which
 * has to be resolvable before its file exists.
 */

export type Tab =
  | 'today'
  | 'shift'
  | 'round'
  | 'vigil'
  | 'relay'
  | 'sage'
  | 'scribe'
  | 'lens'
  | 'forecast'
  | 'appointments'
  | 'surgical-suite'
  | 'admin'
  | 'helix'
  | 'orders'
  | 'pharmacy-verify'
  | 'coding'
  | 'command'
  | 'capacity'
  /* ── The hospital-administration modules (2026-08-11 ruling) ───────────────
   * Orb REPLACES the hospital information system, so it has to carry the parts
   * a hospital cannot run without. Each of these five is a MODULE: a server
   * flag, a nav tab and a lazily-loaded panel, and the tab exists only when
   * BOTH its flag is on and its panel module is in the build. The tab id, the
   * server flag key and the module id are all the same word on purpose — one
   * vocabulary for "is billing on?" across the whole system. */
  | 'billing'
  | 'payments'
  | 'payer'
  | 'procurement'
  | 'frontdesk'
  /* Housekeeping is a hospital module like the others, and it is the one that
   * touches a CLINICAL-ADJACENT number: a bed that is vacant-dirty is not an
   * available bed. It publishes its readiness figures under their own names,
   * beside the capacity screen's, and changes nothing the capacity screen says —
   * see the CAPACITY section of server/services/housekeeping_service.py. */
  | 'housekeeping'
  | 'nabh'
  | 'abdm'
  /* Workforce is the duty roster, staff attendance and the payroll EXPORT. It
   * is deliberately NOT payroll: PF/ESI/PT/TDS are computed by the hospital's
   * payroll provider, and the module's whole shape is that boundary. See
   * server/services/workforce_service.py. */
  | 'workforce'
  /* Dietary is a hospital module by MACHINERY (server flag, lazily-loaded panel)
   * and a CLINICAL surface by content: a diabetic's tray, a renal patient's salt
   * and NIL BY MOUTH are all part of a patient's care. So unlike the money
   * modules below, it is NOT admin-only — see DIETARY_TAB. A nurse who cannot
   * see who is nil by mouth is a nurse who hands somebody breakfast before
   * theatre. */
  | 'dietary'
  /* Biomedical is the equipment register, and it is a hospital module with one
   * clinical edge: Orb INGESTS NUMBERS from the machines it tracks. An
   * uncalibrated monitor is not broken — it works, and it reports the wrong
   * figure, which every layer above it faithfully records and alerts on. The
   * register is the only thing that catches that, so the overdue view is
   * ward-visible (`vitals.read`) while the register itself is administrative.
   * See server/services/biomedical_service.py. */
  | 'biomedical'

/**
 * Rail grouping. Phase 1 renders every group as one flat list — the grouped
 * rail ("Clinical ▸", "Hospital ▸") is Phase 2 and is blocked on the champion's
 * Q1 answer. The grouping is declared now anyway, because the group a tab
 * belongs to is a product decision and recording it here is what makes the
 * later rail trim a config change rather than a redesign.
 */
export type TabGroupId = 'primary' | 'clinical' | 'hospital'

/** The one call-to-action the rail can offer. */
export type PrimaryActionId = 'admit'

/**
 * Which server permission each primary action's request actually requires.
 *
 * THIS, NOT A ROLE LIST, IS THE GATE. `POST /api/admissions/admit` requires
 * `patient.write` (server/routes/admission_routes.py), so the CTA appears iff
 * the account holds `patient.write` — full stop. A role that gains the grant
 * gets the button with no edit here; a role that loses it loses the button the
 * same way. The per-role `primaryAction` field below says only WHERE an action
 * would be offered, never WHETHER it is permitted.
 */
export const PRIMARY_ACTION_PERMISSION: Record<PrimaryActionId, Permission> = {
  admit: PATIENT_WRITE,
}

export interface RoleTab {
  readonly id: Tab
  readonly group: TabGroupId
}

export interface ShellRoleConfig {
  /** Landing tab: the screen this role opens the app on. */
  readonly home: Tab
  /** Ordered as the rail renders them, grouped for the Phase-2 rail trim. */
  readonly tabs: readonly RoleTab[]
  /** The action this experience would offer. Permission still decides. */
  readonly primaryAction: PrimaryActionId | null
}

/* ── The doctor home, wired before it exists ─────────────────────────────────
 *
 * `src/pages/panels/DoctorHomePanel.tsx` is built in parallel with this file.
 * A static `lazy(() => import('./pages/panels/DoctorHomePanel'))` would fail
 * `tsc` and `vite build` outright until that module lands, which would mean the
 * shell could not be verified on its own. `import.meta.glob` resolves the same
 * module through Vite's dependency graph but yields an EMPTY OBJECT when
 * nothing matches, so:
 *
 *   - the panel absent  → 'today' is not a tab, doctors land on 'vigil', and
 *     the shell behaves exactly as it did before this file existed;
 *   - the panel present → the tab registers itself and the doctor's landing
 *     flips to it, with no edit to this file or to ClinicianShell.
 *
 * A tab with no panel is not a tab. That is the invariant, and it is why the
 * doctor home cannot ship as a blank screen if the parallel work slips.
 */
const DOCTOR_HOME_PATH = './pages/panels/DoctorHomePanel.tsx'
const DOCTOR_HOME_MODULES = import.meta.glob('./pages/panels/DoctorHomePanel.tsx')

/** True when the doctor home's module is present in this build. */
export const doctorHomeInstalled: boolean = DOCTOR_HOME_PATH in DOCTOR_HOME_MODULES

export const DoctorHomePanel: LazyExoticComponent<ComponentType> | null = doctorHomeInstalled
  ? lazy(DOCTOR_HOME_MODULES[DOCTOR_HOME_PATH] as () => Promise<{ default: ComponentType }>)
  : null

/* ── The hospital-administration modules ─────────────────────────────────────
 *
 * Five modules — billing, payments, payer, procurement, front desk — each a
 * server flag (config.ORB_BILLING…, ALL DEFAULT ON) plus one panel file. Both
 * halves are built by other agents; this file registers the navigation for them
 * so that no module has to edit the shell's role table.
 *
 * A TAB EXISTS ONLY WHEN BOTH ARE TRUE:
 *
 *   1. its PANEL MODULE is in the build — resolved through `import.meta.glob`,
 *      exactly as the doctor home is (see above). A static import would fail
 *      `tsc` and `vite build` until every panel lands, which would mean the nav
 *      could not be verified on its own and one slipping module would take the
 *      whole app down. A tab with no panel is not a tab.
 *   2. its SERVER FLAG is on — `moduleFlags`, refreshed from /api/health. A site
 *      that runs its own purchasing system sets ORB_PROCUREMENT=0 and the tab is
 *      simply not there; nobody navigates to a screen whose routes 404.
 *
 * Both default to the SAFE direction for this ruling: flags default ON (the
 * ruling is "visible"), panels default absent (a missing file cannot render a
 * blank screen).
 */
export type HospitalModuleId =
  | 'billing' | 'payments' | 'payer' | 'procurement' | 'frontdesk' | 'housekeeping'
  | 'workforce' | 'dietary' | 'biomedical' | 'nabh' | 'abdm'

export const HOSPITAL_MODULE_IDS: readonly HospitalModuleId[] = [
  'billing', 'payments', 'payer', 'procurement', 'frontdesk', 'housekeeping',
  'workforce', 'dietary', 'biomedical', 'nabh', 'abdm',
]

/** Module id → the panel file its agent must create. Paths are literal because
 *  `import.meta.glob` is compile-time: it cannot take a computed pattern. */
export const HOSPITAL_MODULE_PANEL_PATH: Record<HospitalModuleId, string> = {
  billing: './pages/panels/BillingPanel.tsx',
  payments: './pages/panels/PaymentsPanel.tsx',
  payer: './pages/panels/PayerPanel.tsx',
  procurement: './pages/panels/ProcurementPanel.tsx',
  frontdesk: './pages/panels/FrontDeskPanel.tsx',
  housekeeping: './pages/panels/HousekeepingPanel.tsx',
  workforce: './pages/panels/WorkforcePanel.tsx',
  dietary: './pages/panels/DietaryPanel.tsx',
  biomedical: './pages/panels/BiomedicalPanel.tsx',
  nabh: './pages/panels/NabhPanel.tsx',
  abdm: './pages/panels/AbdmPanel.tsx',
}

const HOSPITAL_MODULE_MODULES = import.meta.glob([
  './pages/panels/BillingPanel.tsx',
  './pages/panels/PaymentsPanel.tsx',
  './pages/panels/PayerPanel.tsx',
  './pages/panels/ProcurementPanel.tsx',
  './pages/panels/FrontDeskPanel.tsx',
  './pages/panels/HousekeepingPanel.tsx',
  './pages/panels/WorkforcePanel.tsx',
  './pages/panels/DietaryPanel.tsx',
  './pages/panels/BiomedicalPanel.tsx',
  './pages/panels/NabhPanel.tsx',
  './pages/panels/AbdmPanel.tsx',
])

/** True for each module whose panel file is present in THIS build. */
export const hospitalModuleInstalled: Record<HospitalModuleId, boolean> =
  Object.fromEntries(
    HOSPITAL_MODULE_IDS.map(id => [id, HOSPITAL_MODULE_PANEL_PATH[id] in HOSPITAL_MODULE_MODULES]),
  ) as Record<HospitalModuleId, boolean>

/** The lazy component for each installed module panel; null when absent. */
export const HOSPITAL_MODULE_PANELS: Record<HospitalModuleId, LazyExoticComponent<ComponentType> | null> =
  Object.fromEntries(
    HOSPITAL_MODULE_IDS.map(id => {
      const loader = HOSPITAL_MODULE_MODULES[HOSPITAL_MODULE_PANEL_PATH[id]]
      return [id, loader ? lazy(loader as () => Promise<{ default: ComponentType }>) : null]
    }),
  ) as Record<HospitalModuleId, LazyExoticComponent<ComponentType> | null>

/** The panel to mount for a tab id, or null if it is not a module tab / not
 *  installed. This is the shell's one lookup — it never names a module panel. */
export function modulePanel(id: Tab): LazyExoticComponent<ComponentType> | null {
  return (HOSPITAL_MODULE_PANELS as Record<string, LazyExoticComponent<ComponentType> | null>)[id] ?? null
}

/* ── Module flags ─────────────────────────────────────────────────────────────
 *
 * The server owns the truth (config.enabled_modules, published on /api/health).
 * This is the client's CACHE of that answer, and it defaults to all-ON so that
 * the very first paint — before any network round trip — matches the documented
 * default rather than hiding modules a site actually runs.
 *
 * Because the default is ON, an unrefreshed cache can only fail toward VISIBLE:
 * a tab appears whose routes are disabled, and the panel says so. The opposite
 * default would silently hide a module a hospital paid for, which is the worse
 * failure and the harder one to notice.
 */
const DEFAULT_MODULE_FLAGS: Record<HospitalModuleId, boolean> = {
  billing: true, payments: true, payer: true, procurement: true, frontdesk: true,
  housekeeping: true, workforce: true,
  dietary: true, biomedical: true, nabh: true, abdm: true,
}

/**
 * Build-time / runtime overrides, read the same way the landing-tab override is:
 *
 *   window.__ORB_MODULES__ = { procurement: false }   // index.html / ops
 *   VITE_ORB_MODULES=billing,payments                 // allow-list at build time
 *
 * The env form is an ALLOW-LIST (anything not named is off) because that is what
 * "this build ships these modules" means; the runtime object is a patch.
 */
export function readModuleOverrides(): Partial<Record<HospitalModuleId, boolean>> {
  const out: Partial<Record<HospitalModuleId, boolean>> = {}
  const envList = (import.meta.env?.VITE_ORB_MODULES as string | undefined)?.trim()
  if (envList) {
    const allowed = new Set(envList.split(',').map(s => s.trim()).filter(Boolean))
    for (const id of HOSPITAL_MODULE_IDS) out[id] = allowed.has(id)
  }
  const runtime = typeof window !== 'undefined' ? window.__ORB_MODULES__ : undefined
  if (runtime) {
    for (const id of HOSPITAL_MODULE_IDS) {
      if (typeof runtime[id] === 'boolean') out[id] = runtime[id]
    }
  }
  return out
}

let moduleFlags: Record<HospitalModuleId, boolean> = {
  ...DEFAULT_MODULE_FLAGS,
  ...readModuleOverrides(),
}

const moduleFlagListeners = new Set<() => void>()

/** The flags the shell should render with right now. */
export function currentModuleFlags(): Readonly<Record<HospitalModuleId, boolean>> {
  return moduleFlags
}

/** Apply a partial update (from /api/health, or from a test) and notify. */
export function setModuleFlags(next: Partial<Record<HospitalModuleId, boolean>>): void {
  const merged = { ...moduleFlags }
  let changed = false
  for (const id of HOSPITAL_MODULE_IDS) {
    if (typeof next[id] === 'boolean' && merged[id] !== next[id]) {
      merged[id] = next[id] as boolean
      changed = true
    }
  }
  if (!changed) return
  moduleFlags = merged
  for (const listener of moduleFlagListeners) listener()
}

/** Reset to the compile-time defaults + overrides. For tests and sign-out. */
export function resetModuleFlags(): void {
  moduleFlags = { ...DEFAULT_MODULE_FLAGS, ...readModuleOverrides() }
  for (const listener of moduleFlagListeners) listener()
}

/** Subscribe to flag changes — `useSyncExternalStore`-shaped on purpose. */
export function subscribeModuleFlags(listener: () => void): () => void {
  moduleFlagListeners.add(listener)
  return () => { moduleFlagListeners.delete(listener) }
}

/**
 * Ask the server which modules this deployment runs.
 *
 * Unauthenticated (/api/health carries no PHI) so it can run before login, which
 * is what lets the rail be correct on first paint rather than flickering a tab
 * away a second after the user sees it. NEVER THROWS: a health endpoint that is
 * unreachable leaves the defaults in place, because a network blip must not be
 * able to remove a hospital's billing screen.
 */
export async function refreshModuleFlags(
  fetchImpl: typeof fetch | undefined = typeof fetch === 'function' ? fetch : undefined,
  base = '/api',
): Promise<Readonly<Record<HospitalModuleId, boolean>>> {
  if (!fetchImpl) return currentModuleFlags()
  try {
    const resp = await fetchImpl(`${base}/health`, { headers: { Accept: 'application/json' } })
    if (!resp.ok) return currentModuleFlags()
    const body = await resp.json() as { modules?: Partial<Record<HospitalModuleId, boolean>> }
    if (body?.modules) setModuleFlags(body.modules)
  } catch {
    /* Offline, server down, or a proxy returning HTML. Keep the defaults. */
  }
  return currentModuleFlags()
}

/* ── Tab metadata ─────────────────────────────────────────────────────────── */

export interface TabDef {
  readonly id: Tab
  /** English fallback. The rendered label is always `t('nav.<id>')`. */
  readonly label: string
  readonly pillar: string
  readonly icon: ReactNode
}

export const TABS: readonly TabDef[] = [
  {
    id: 'today', label: 'Today', pillar: 'Today',
    icon: (
      // A day with its work marked on it — the doctor's first minute, not a clock.
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="4" width="18" height="17" rx="2"/><path d="M3 9h18"/>
        <path d="M8 2v4"/><path d="M16 2v4"/><path d="M8 14l2.5 2.5L16 12"/>
      </svg>
    ),
  },
  {
    id: 'shift', label: 'My Shift', pillar: 'Nurse Mode',
    icon: (
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M9 11l2 2 4-4"/><rect x="4" y="4" width="16" height="17" rx="2"/><path d="M9 2h6v4H9z"/>
      </svg>
    ),
  },
  {
    id: 'helix', label: 'Pharmacy', pillar: 'Helix',
    icon: (
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M10 2v7.31"/><path d="M14 9.3V1.99"/><path d="M8.5 2h7"/><path d="M14 9.3a6.5 6.5 0 1 1-4 0"/><path d="M5.52 16h12.96"/>
      </svg>
    ),
  },
  {
    id: 'pharmacy-verify', label: 'Verify Queue', pillar: 'Pharmacy Verification',
    icon: (
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/>
      </svg>
    ),
  },
  {
    id: 'vigil', label: 'Patients', pillar: 'Vigil',
    icon: (
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 12h4l3-9 4 18 3-9h4"/>
      </svg>
    ),
  },
  {
    id: 'round', label: 'Ward Round', pillar: 'Ward Round',
    icon: (
      // Beds along a corridor, walked in order — the round is a route, not a list.
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="5" cy="6" r="2.2"/><circle cx="12" cy="6" r="2.2"/><circle cx="19" cy="6" r="2.2"/>
        <path d="M3 13h18"/><path d="M6 17l3 3 6-6"/>
      </svg>
    ),
  },
  {
    id: 'orders', label: 'Orders', pillar: 'Orders',
    icon: (
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><path d="M9 13h6"/><path d="M9 17h6"/>
      </svg>
    ),
  },
  {
    id: 'relay', label: 'Messages', pillar: 'Relay',
    icon: (
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
      </svg>
    ),
  },
  {
    id: 'sage', label: 'Sage', pillar: 'Sage',
    icon: (
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="10"/>
        <path d="M12 16v-4M12 8h.01"/>
      </svg>
    ),
  },
  {
    id: 'scribe', label: 'Scribe', pillar: 'Scribe',
    icon: (
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
        <polyline points="14 2 14 8 20 8"/>
        <line x1="16" y1="13" x2="8" y2="13"/>
        <line x1="16" y1="17" x2="8" y2="17"/>
      </svg>
    ),
  },
  {
    id: 'lens', label: 'Lens', pillar: 'Lens',
    icon: (
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="11" cy="11" r="8"/>
        <line x1="21" y1="21" x2="16.65" y2="16.65"/>
        <circle cx="11" cy="11" r="3"/>
      </svg>
    ),
  },
  {
    id: 'forecast', label: 'Forecast', pillar: 'Forecast',
    icon: (
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
        <line x1="2" y1="20" x2="22" y2="20" strokeDasharray="3,3" opacity="0.5"/>
      </svg>
    ),
  },
  {
    id: 'command', label: 'Command', pillar: 'Command Center',
    icon: (
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>
      </svg>
    ),
  },
  {
    id: 'capacity', label: 'Simulator', pillar: 'What-If Simulator',
    icon: (
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 3v18h18"/><path d="M7 14l3-4 3 3 4-6"/><circle cx="20" cy="7" r="1.3" fill="currentColor" stroke="none"/>
      </svg>
    ),
  },
  {
    id: 'coding', label: 'Revenue', pillar: 'Revenue Integrity',
    icon: (
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        {/* Indian rupee (lucide indian-rupee): this hospital bills in rupees. */}
        <path d="M6 3h12" /><path d="M6 8h12" /><path d="m6 13 8.5 8" /><path d="M6 13h3" /><path d="M9 13c6.667 0 6.667-10 0-10" />
      </svg>
    ),
  },
  {
    id: 'appointments', label: 'Appointments', pillar: 'Appointments',
    icon: (
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="4" width="18" height="18" rx="2" ry="2"/>
        <line x1="16" y1="2" x2="16" y2="6"/>
        <line x1="8" y1="2" x2="8" y2="6"/>
        <line x1="3" y1="10" x2="21" y2="10"/>
        <path d="M8 14h.01"/><path d="M12 14h.01"/><path d="M16 14h.01"/><path d="M8 18h.01"/><path d="M12 18h.01"/>
      </svg>
    ),
  },
  {
    id: 'surgical-suite', label: 'Surgical Suite', pillar: 'Surgical Suite',
    icon: (
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M18 2l-2.5 9H8.5L5.5 2"/>
        <path d="M12 11v6"/>
        <path d="M8 22h8"/>
        <path d="M12 17h0"/>
        <circle cx="12" cy="6" r="1" fill="currentColor" stroke="none"/>
        <path d="M7 2h10" strokeWidth="2"/>
      </svg>
    ),
  },
  {
    id: 'admin', label: 'Admin', pillar: 'Admin',
    icon: (
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
      </svg>
    ),
  },
  {
    id: 'billing', label: 'Billing', pillar: 'Billing',
    icon: (
      // A bill: a document with amounts ruled off at the foot.
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M6 2h12a1 1 0 0 1 1 1v18l-3-2-2 2-2-2-2 2-2-2-3 2V3a1 1 0 0 1 1-1z"/>
        <path d="M9 7h6"/><path d="M9 11h6"/><path d="M9 15h3"/>
      </svg>
    ),
  },
  {
    id: 'payments', label: 'Payments', pillar: 'Payments',
    icon: (
      // A receipt handed across a counter — money taken, not money owed.
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <rect x="2" y="6" width="20" height="12" rx="2"/>
        <circle cx="12" cy="12" r="2.5"/><path d="M6 12h.01"/><path d="M18 12h.01"/>
      </svg>
    ),
  },
  {
    id: 'payer', label: 'Insurance', pillar: 'Insurance & claims',
    icon: (
      // A shield with a document in it: cover, and the paperwork that proves it.
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 21s7-3.5 7-9V5.5L12 3 5 5.5V12c0 5.5 7 9 7 9z"/>
        <path d="M9 10h6"/><path d="M9 13.5h4"/>
      </svg>
    ),
  },
  {
    id: 'procurement', label: 'Procurement', pillar: 'Procurement & Stores',
    icon: (
      // A carton on a shelf — goods received, not drugs dispensed.
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 8l9-4 9 4v8l-9 4-9-4z"/><path d="M3 8l9 4 9-4"/><path d="M12 12v8"/>
      </svg>
    ),
  },
  {
    id: 'frontdesk', label: 'Front Desk', pillar: 'Front Desk',
    icon: (
      // A counter with somebody standing at it.
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="6" r="2.5"/><path d="M8 12h8a2 2 0 0 1 2 2v1H6v-1a2 2 0 0 1 2-2z"/>
        <path d="M3 18h18"/><path d="M5 18v3"/><path d="M19 18v3"/>
      </svg>
    ),
  },
  {
    // The topbar prints "Orb <pillar>", so the pillar is the sidebar's word:
    // three names for one screen (Accreditation, NABH Evidence & Quality, NABH
    // accreditation evidence) read as three places.
    id: 'nabh', label: 'Accreditation', pillar: 'Accreditation',
    icon: (
      // A rosette: the accreditation award itself. Deliberately not a clipboard
      // — every other back-office tab could be a clipboard, and this one is
      // about the standard being met, not the paperwork.
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="9" r="5" />
        <path d="M9 13.5 7.5 21l4.5-2.5L16.5 21 15 13.5" />
      </svg>
    ),
  },
  {
    id: 'abdm', label: 'ABDM', pillar: 'ABDM',
    icon: (
      // Two linked rings: the care-context LINK is what ABDM is, and what this
      // module records. Not a flag, not a government building.
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M9.5 14.5a4 4 0 0 1 0-5.7l2-2a4 4 0 0 1 5.7 5.7l-1 1" />
        <path d="M14.5 9.5a4 4 0 0 1 0 5.7l-2 2a4 4 0 0 1-5.7-5.7l1-1" />
      </svg>
    ),
  },
  {
    id: 'housekeeping', label: 'Housekeeping', pillar: 'Housekeeping & Facilities',
    icon: (
      // A stripped bed with a fresh sheet going on: the turnaround itself, not a
      // mop or a bucket. The thing this module measures is the bed being out of
      // action, and the icon should be of the bed.
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3 18v-8"/><path d="M21 18v-5a2 2 0 0 0-2-2H8"/>
        <path d="M3 14h18"/><circle cx="6.5" cy="8.5" r="1.8"/><path d="M3 18h18"/>
      </svg>
    ),
  },
  {
    id: 'workforce', label: 'Workforce', pillar: 'Duty Roster & Attendance',
    icon: (
      // Two people against a clock face: this module is about WHO and WHEN, and
      // never about money. Deliberately not a rupee, a payslip or a calculator —
      // the icon should not promise a payroll screen the module refuses to be.
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M9 11a3.2 3.2 0 1 0 0-6.4A3.2 3.2 0 0 0 9 11z"/>
        <path d="M2.5 20v-1.4A4.1 4.1 0 0 1 6.6 14.5h4.8"/>
        <circle cx="17" cy="16" r="5"/><path d="M17 13.6V16l1.7 1.1"/>
      </svg>
    ),
  },
  {
    id: 'dietary', label: 'Diet & Kitchen', pillar: 'Diet & Kitchen',
    icon: (
      // A tray with a cloche — the thing that actually arrives at the bedside.
      // Deliberately not cutlery-as-restaurant and not a chef's hat: what this
      // module governs is a patient's meal as part of their treatment, and the
      // one state it must never make look ordinary is nil by mouth.
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 16h16"/>
        <path d="M5.5 16a6.5 6.5 0 0 1 13 0"/>
        <path d="M12 7V5.5"/>
        <path d="M3 19h18"/>
      </svg>
    ),
  },
  {
    id: 'biomedical', label: 'Equipment', pillar: 'Biomedical Equipment',
    icon: (
      // A gauge with its needle past the mark — a MEASURING instrument, and one
      // that is reading. Deliberately not a spanner: a spanner says "repairs",
      // and the thing this module is actually for is the machine that is not
      // broken and is quietly reporting the wrong number.
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M3.5 17a9 9 0 1 1 17 0"/>
        <path d="M12 17l4.2-4.6"/>
        <circle cx="12" cy="17" r="1.4"/>
        <path d="M3.5 20h17"/>
      </svg>
    ),
  },
]

const TAB_BY_ID = new Map(TABS.map(tab => [tab.id, tab]))

export function tabDef(id: Tab): TabDef | undefined {
  return TAB_BY_ID.get(id)
}

/* ── Per-role configuration ───────────────────────────────────────────────── */

const t = (id: Tab, group: TabGroupId = 'primary'): RoleTab => ({ id, group })

/**
 * The doctor's rail, in the order it renders.
 *
 * 'today' leads and is the home: the doctor's first minute is a COMPOSITION —
 * who is deteriorating, what needs my signature, what came back — which no
 * single panel can answer (DUAL_EXPERIENCE_DESIGN.md §1.3.1). Ward Round stays
 * third rather than first for the reason recorded when this was a switch
 * statement: a doctor is not always rounding, it is roughly an hour of a long
 * day, so the round is one tap away and never the thing the app opens on.
 */
const DOCTOR_TABS: readonly RoleTab[] = [
  t('today'), t('vigil'), t('round'), t('orders'), t('relay'), t('sage'), t('scribe'),
  t('lens', 'clinical'), t('forecast', 'clinical'),
  t('command', 'hospital'), t('capacity', 'hospital'), t('coding', 'hospital'),
]

/**
 * The five administration tabs, in the order the Hospital group renders them,
 * and WHO GETS THEM.
 *
 * THEY GO TO `admin` ONLY. Not doctors, not nurses. Three reasons, and the
 * first is the one that matters for a pilot:
 *
 *   1. ADJACENCY IS A SAFETY PROPERTY. The pilot's whole premise is that a
 *      financial rule can never touch a clinical action. Putting "raise an
 *      invoice" one tap from "administer a dose" in the same rail is how the two
 *      start to feel like the same kind of act, and how a nurse ends up
 *      answering a billing question in the middle of a medication round.
 *   2. NOBODY ASKED THEM TO DO THIS WORK. A nurse does not raise a purchase
 *      order. A doctor does not issue a GST invoice or chase a TPA denial. These
 *      screens belong to the billing counter, the stores clerk and the insurance
 *      desk — and in Orb's user model (`users.role` allows only doctor, nurse,
 *      admin, patient, pharmacist, cashier) every one of those people holds an
 *      `admin` account, except the cashier, who has a role of their own and
 *      gets Payments alone (ROLE_CONFIG below). Giving the tab to the role that
 *      does the job IS "visible by default"; spraying it at every role is not.
 *   3. A TAB THAT 403s IS WORSE THAN NO TAB. That is the exact defect this
 *      file was built to kill (the admit-CTA drift, §1.2.2). Server permissions
 *      for these modules are the module agents' to define; until a clinical role
 *      demonstrably holds them, the honest rail does not offer the screen.
 *
 * WHAT THIS DELIBERATELY LEAVES OPEN: ward staff DO legitimately raise indents
 * for ward consumables, and a doctor DOES need to see whether a patient's cover
 * permits a procedure. Both are real and neither is a whole module — they are
 * narrow, permission-scoped surfaces that belong inside the clinical screens
 * that already exist (a "request from stores" action on the ward board; the
 * cover status on the patient header), not a Procurement tab on a nurse's rail.
 * Recorded as a followup rather than solved by handing over the tab.
 */
const HOSPITAL_MODULE_TABS: readonly RoleTab[] = [
  t('frontdesk', 'hospital'),
  t('billing', 'hospital'),
  t('payments', 'hospital'),
  t('payer', 'hospital'),
  t('procurement', 'hospital'),
  /* Housekeeping goes to `admin` with the rest, and that is a decision worth
   * recording rather than a copy-paste.
   *
   * The person who OWNS a hospital's turnaround is the housekeeping supervisor
   * or the bed manager, and in Orb's user model (`users.role` allows only
   * doctor, nurse, admin, patient, pharmacist, cashier) both hold an `admin` account. So
   * `admin` is the role that does this job.
   *
   * The ward NURSE is the open question, and it is a real one: she is the person
   * who knows the patient has gone, and the server already lets her act — the
   * write dependency is `vitals.write` OR `admin.overview`, so a nurse can mark
   * a bed dirty through the API today. What she does not have is the TAB. That
   * is deliberate for tonight (the nurse rail is being rebuilt in parallel and a
   * facilities tab is a product decision, not a merge), and it is the one thing
   * that would most improve this module's data quality, because a turnaround
   * clock that starts when an administrator notices is a slower clock than one
   * that starts when the nurse says the bed is empty. Recorded as a followup for
   * the champion, not smuggled in here. */
  t('housekeeping', 'hospital'),
  /* NABH evidence and ABDM rails are COMPLIANCE surfaces: an accreditation
   * pack and the national-health-stack record. Admin-only for the same
   * reason as the money screens — a clinician neither files them nor is
   * measured usefully by having them one tap from a dose. */
  t('nabh', 'hospital'),
  t('abdm', 'hospital'),
  /* Workforce goes to `admin` only, and here the reasoning is sharper than for
   * the other modules rather than the same.
   *
   * This screen holds every employee's attendance and leave. That is personal
   * data about COLLEAGUES — who was sick, who took leave without pay, who was
   * late eleven times — and it is exactly the kind of record that must be
   * visible to the people whose job it is and to nobody else. Handing the tab to
   * a clinical role would not be "visible by default"; it would be publishing a
   * ward's leave register to the ward.
   *
   * What that leaves open, and it is a real gap: the ward IN-CHARGE is the
   * person who actually knows who turned up, and she cannot record it — every
   * route requires `admin.overview`. Until a `workforce.attendance` permission
   * exists, attendance is entered by administration from the ward's own muster,
   * which is how most Indian hospitals already work and is therefore usable, but
   * it is not where this should end up. Recorded as a followup, with the same
   * note in server/routes/workforce_routes.py. */
  t('workforce', 'hospital'),
  /* Biomedical goes to `admin` only, and the reason is the permission the panel
   * needs rather than the sensitivity of what it holds: every register route
   * requires `helix.read` / `helix.write`, which admin and pharmacist hold and
   * no clinical role does.
   *
   * WHAT THAT LEAVES OPEN, and it is the gap that matters for this module: the
   * WARD is who needs to know its monitor is out of calibration, and the ward
   * view (`GET /api/biomedical/wards/{ward}/attention`, `vitals.read`, no PHI in
   * the payload) is built and reachable by every clinical role — but there is no
   * TAB or card that surfaces it, so today a nurse can only reach it through the
   * API. Putting an equipment strip on the nurse home is a product decision
   * about that screen's attention budget, not a merge, so it is recorded as a
   * followup rather than smuggled in here. Until then the ward is told by the
   * biomedical engineer, which is how hospitals work today and is slower than it
   * should be. Same note in server/routes/biomedical_routes.py. */
  t('biomedical', 'hospital'),
]

/**
 * DIET & KITCHEN GOES TO THE WARD, NOT ONLY TO THE OFFICE.
 *
 * Every other module tab in this file is `admin`-only, and the reason written
 * above is a real safety property: "raise an invoice" must not sit one tap from
 * "administer a dose". Dietary is the exception, and the exception is decided on
 * exactly the same principle rather than in spite of it.
 *
 * A diet order IS a clinical order. A diabetic's carbohydrate load, a renal
 * patient's potassium, a stroke patient's swallow-safe texture and NIL BY MOUTH
 * before theatre are treatment decisions that happen to arrive on a tray. The
 * people who write them are doctors and nurses (the server gates the write on
 * `order.write`, which only they hold), and the person who most needs to see who
 * cannot eat is the nurse walking the ward at 07:00 with a trolley. An
 * admin-only Diet tab would put that screen in an office and the trolley in a
 * corridor, which is precisely the paper-diet-sheet failure this module exists
 * to remove.
 *
 * The catering desk still gets it — an `admin` account holds `admin.overview`
 * and can run the counts and the indent — but it CANNOT change what a patient is
 * allowed to eat. That split lives in server/routes/dietary_routes.py, and the
 * rail here simply reflects it.
 *
 * Grouped 'clinical', not 'hospital', for the same reason.
 */
const DIETARY_TAB: RoleTab = t('dietary', 'clinical')

/**
 * The modules whose CONTENT is clinical, and which may therefore appear on a
 * clinician's rail. Everything else in HOSPITAL_MODULE_IDS is a money or
 * back-office screen and is admin-only.
 *
 * THIS LIST EXISTS TO BE SHORT AND TO BE GUARDED. Until dietary landed, "no
 * clinical role sees a hospital-module tab" and "no clinical role sees a money
 * screen" were the same sentence, and the shell's test asserted the first while
 * meaning the second. They are no longer the same sentence, so the distinction
 * is written down here and pinned by shellConfig.test.ts: adding an id to this
 * array is how somebody would put "raise an invoice" one tap from "administer a
 * dose", and the test names the exact permitted set so that edit cannot pass
 * quietly.
 *
 * The bar for membership: a clinician WRITES the thing the module is about, and
 * the server gates that write on a clinical permission. Diet orders qualify —
 * `POST /api/dietary/orders` requires `order.write`, which only doctors and
 * nurses hold. Billing, payments, payer, procurement, front desk, housekeeping
 * and workforce do not and must not.
 */
export const CLINICAL_MODULE_IDS: readonly HospitalModuleId[] = ['dietary']

/** The admin-only remainder: the money and back-office screens. */
export const MONEY_MODULE_IDS: readonly HospitalModuleId[] =
  HOSPITAL_MODULE_IDS.filter(id => !CLINICAL_MODULE_IDS.includes(id))

const ROLE_CONFIG: Record<string, ShellRoleConfig> = {
  doctor: {
    home: 'today',
    tabs: [...DOCTOR_TABS, t('appointments', 'clinical'), DIETARY_TAB],
    primaryAction: 'admit',
  },
  /* Server alias: `ROLE_PERMISSIONS["clinician"] = ROLE_PERMISSIONS["doctor"]`
     (server/permissions.py:168), and src/lib/roles.ts mirrors it. The shell used
     to let 'clinician' fall through to the unknown-role default and show two
     tabs to an account holding every doctor permission — a drift in the same
     family as the admit CTA. Aliased here for the same reason it is aliased
     there: one role, one answer. */
  clinician: {
    home: 'today',
    tabs: [...DOCTOR_TABS, t('appointments', 'clinical'), DIETARY_TAB],
    primaryAction: 'admit',
  },
  /* 'surgeon' is a safety net for a raw backend role Login normally normalizes
     to doctor + department Surgery; a direct/refresh path can still surface it.
     It keeps the pre-existing reduced set — including no 'today', because the
     home is registered for the normalized doctor role, and no admit CTA,
     because the server has no 'surgeon' key and would refuse the write. */
  surgeon: {
    home: 'vigil',
    tabs: [
      t('vigil'), t('round'), t('relay'), t('sage'), t('scribe'),
      t('lens', 'clinical'), t('forecast', 'clinical'), t('surgical-suite'),
    ],
    primaryAction: null,
  },
  /* 'shift' leads for nurses and is their home: a nurse coming on shift needs
     the task list, not the patient roster. */
  nurse: {
    home: 'shift',
    tabs: [
      t('shift'), t('vigil'), t('orders'), t('relay'), t('sage'), t('scribe'),
      t('lens', 'clinical'), t('forecast', 'clinical'),
      t('coding', 'hospital'), t('appointments', 'clinical'), DIETARY_TAB,
    ],
    primaryAction: 'admit',
  },
  admin: {
    home: 'vigil',
    tabs: [
      t('vigil'), t('relay'), t('sage'), t('forecast', 'clinical'),
      t('command', 'hospital'), t('capacity', 'hospital'), t('coding', 'hospital'),
      ...HOSPITAL_MODULE_TABS,
      DIETARY_TAB, t('appointments', 'clinical'), t('admin', 'hospital'),
    ],
    primaryAction: 'admit',
  },
  pharmacist: {
    home: 'helix',
    tabs: [t('helix'), t('pharmacy-verify'), t('sage')],
    primaryAction: null,
  },
  /* The cash counter as its own account (server/permissions.py "cashier":
     payments.read, .record, .close_counter and nothing else). Payments is the
     whole job and the only screen the server will answer for this account, so
     it is the only tab. Without this entry a cashier fell through to the
     unknown-role fallback: Vigil and Relay, both 403, and no Payments at all.
     No admit CTA: a cashier does not hold patient.write. */
  cashier: {
    home: 'payments',
    tabs: [t('payments', 'hospital')],
    primaryAction: null,
  },
}

/** An unknown or absent role gets the smallest honest surface, never a blank
 *  shell: the ward board and messages, which is what it has always got. */
const FALLBACK_CONFIG: ShellRoleConfig = {
  home: 'vigil',
  tabs: [t('vigil'), t('relay')],
  primaryAction: 'admit',
}

/* ── Per-deployment overrides ─────────────────────────────────────────────── */

/**
 * A site may move a role's landing tab.
 *
 * The doctor home defaults ON, per DUAL_EXPERIENCE_DESIGN.md §7 Q2 — during the
 * shadow phase clinicians are not in yet, so the feedback is free. But training
 * materials are written at Weeks 9–10 and **training must never describe a
 * screen that moved last week**, so a site that has already trained its doctors
 * on the roster must be able to put them back on it without a code change:
 *
 *   window.__ORB_SHELL__ = { home: { doctor: 'vigil' } }   // index.html / ops
 *   VITE_ORB_HOME_DOCTOR=vigil                            // build-time
 *
 * An override naming a tab the role cannot reach is ignored, not obeyed — a
 * config typo must not be able to land a doctor on a tab they have no
 * permission for, or on a blank screen.
 */
export interface ShellOverrides {
  readonly home?: Readonly<Record<string, string>>
}

declare global {
  interface Window {
    __ORB_SHELL__?: ShellOverrides
    /** Per-deployment module switches, patched over the server's answer. */
    __ORB_MODULES__?: Partial<Record<HospitalModuleId, boolean>>
  }
}

export function readDeploymentOverrides(): ShellOverrides {
  const home: Record<string, string> = {}
  const envHome = (import.meta.env?.VITE_ORB_HOME_DOCTOR as string | undefined)?.trim()
  if (envHome) {
    home.doctor = envHome
    home.clinician = envHome
  }
  const runtime = typeof window !== 'undefined' ? window.__ORB_SHELL__ : undefined
  if (runtime?.home) Object.assign(home, runtime.home)
  return { home }
}

/* ── Derivation — the only readers the shell has ──────────────────────────── */

function normalizeRole(role: string | null | undefined): string {
  return (role || '').toLowerCase().trim()
}

/**
 * The role's configuration, with tabs whose panel is not installed removed.
 *
 * `department` selects the surgeon's extra tab exactly as the old switch did:
 * a doctor in a surgery department trades Appointments for the Surgical Suite.
 *
 * `modules` and `installed` are injectable ON PURPOSE. `installed` is a property
 * of the BUILD (which panel files exist), so a test that reads it from the build
 * asserts nothing while those files are absent and asserts something different
 * once they land — a test that silently changes meaning. Passing both in lets
 * "a switched-off module has no tab" and "no clinical role gets a money screen"
 * be tested for real today, and keeps meaning the same thing tomorrow. The shell
 * never passes them; only tests do.
 */
export function getRoleConfig(
  role: string | null | undefined,
  department?: string,
  modules: Readonly<Record<HospitalModuleId, boolean>> = currentModuleFlags(),
  installed: Readonly<Record<HospitalModuleId, boolean>> = hospitalModuleInstalled,
): ShellRoleConfig {
  const key = normalizeRole(role)
  const base = ROLE_CONFIG[key] ?? FALLBACK_CONFIG
  const isSurgeon = department === 'Surgery' || department === 'General Surgery'
  const tabs = (isSurgeon && (key === 'doctor' || key === 'clinician')
    ? [...DOCTOR_TABS, t('surgical-suite')]
    : base.tabs
  )
    .filter(entry => entry.id !== 'today' || doctorHomeInstalled)
    /* A hospital-module tab survives only if its flag is on AND its panel is in
       the build. Either half missing means the tab is not there at all — never a
       tab that opens onto nothing, and never a tab for a module this deployment
       switched off. */
    .filter(entry => !isHospitalModuleId(entry.id)
      || (modules[entry.id] !== false && installed[entry.id]))
  const home = tabs.some(entry => entry.id === base.home) ? base.home : tabs[0]?.id ?? 'vigil'
  return { ...base, tabs, home }
}

function isHospitalModuleId(id: Tab): id is HospitalModuleId {
  return (HOSPITAL_MODULE_IDS as readonly string[]).includes(id)
}

/** The ordered tab ids this role may open. The rail, the keyboard layer and the
 *  navigation bus are all filtered through this one answer. */
export function getAccessibleTabs(
  role: string | null | undefined,
  department?: string,
  modules: Readonly<Record<HospitalModuleId, boolean>> = currentModuleFlags(),
  installed: Readonly<Record<HospitalModuleId, boolean>> = hospitalModuleInstalled,
): Tab[] {
  return getRoleConfig(role, department, modules, installed).tabs.map(entry => entry.id)
}

/**
 * The tab the app opens on, after per-deployment overrides.
 *
 * Falls back to the first accessible tab — never to a hard-coded id — so a role
 * whose home is unavailable lands on something it can actually see.
 */
export function getLandingTab(
  role: string | null | undefined,
  department?: string,
  overrides: ShellOverrides = readDeploymentOverrides(),
): Tab {
  const config = getRoleConfig(role, department)
  const requested = overrides.home?.[normalizeRole(role)]
  if (requested && config.tabs.some(entry => entry.id === requested)) return requested as Tab
  return config.home
}

/**
 * The primary CTA this account may actually use, or null.
 *
 * Two conditions, and the permission is the one that matters: the experience
 * has to place the action, AND the account has to hold the permission the
 * request requires. Never a role list — see PRIMARY_ACTION_PERMISSION.
 */
export function getPrimaryAction(
  role: string | null | undefined,
  department?: string,
): PrimaryActionId | null {
  const action = getRoleConfig(role, department).primaryAction
  if (!action) return null
  /* `role` is forwarded exactly as the shell holds it, including `undefined`:
     hasPermission's default parameter then reads the stored session, which is
     the behaviour the sidebar has shipped. An explicit `null` still holds
     nothing — see the note on hasPermission. */
  return hasPermission(PRIMARY_ACTION_PERMISSION[action], role) ? action : null
}
