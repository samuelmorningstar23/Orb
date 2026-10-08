import { lazy, useSyncExternalStore, type ComponentType, type LazyExoticComponent, type ReactNode } from 'react'
import {
  hasPermission, PATIENT_WRITE, ROLE_FEATURE, ROLE_FEATURE_ALSO, VITALS_TRANSCRIBE, type Permission, type RoleFeatureId,
} from './lib/roles'

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
  /* ── The opt-in hospital modules (HIS build plan, decision X1, 3 October 2026)
   * Beds and admissions, the pharmacy store and the laboratory are modules by
   * the same machinery as the eleven above (server flag, lazily-loaded panel),
   * with one difference Samuel ruled on 3 October 2026: they default OFF. A
   * site switches each one on (ORB_ADT, ORB_PHARMACY_STORE, ORB_LAB). See
   * OPT_IN_MODULE_IDS below. */
  | 'adt'
  | 'pharmacy-store'
  | 'lab'
  /* ── Feature tabs ──────────────────────────────────────────────────────────
   * A feature tab is gated by a server FEATURE flag (published as 'features' on
   * /api/health, all default off) rather than by a module flag, and like a
   * module tab it exists only when its panel file is in the build. OPD is the
   * first: the doctor's outpatient queue and consultation screen. See
   * FEATURE_TAB_IDS below. */
  | 'opd'
  /* The ward clerk's own Observations screen (Orb Ward Record, 9 October
   * 2026): she copies each round from the paper sheet. A PERMISSION tab (see
   * PERMISSION_TABS): shown to an account holding vitals.transcribe, with the
   * beds module on or off. */
  | 'observations'

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
  /** False when this experience carries no floating Sage assistant. Absent
   *  means it does, which is every role that existed before the HIS roles. The
   *  HIS roles hold no sage.query (rulings OPD-N3, LIS-R12; the ward clerk's
   *  grant is the bed board only), so an orb on their screen would refuse every
   *  question it was asked. */
  readonly floatingSage?: boolean
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
  | 'adt' | 'pharmacy-store' | 'lab'

export const HOSPITAL_MODULE_IDS: readonly HospitalModuleId[] = [
  'billing', 'payments', 'payer', 'procurement', 'frontdesk', 'housekeeping',
  'workforce', 'dietary', 'biomedical', 'nabh', 'abdm',
  'adt', 'pharmacy-store', 'lab',
]

/**
 * THE OPT-IN MODULES, which default OFF.
 *
 * Every other module follows the 2026-08-11 ruling, "build it all and ship it
 * visible": its flag defaults ON. On 3 October 2026 Samuel departed from that
 * ruling for these three only (HIS build plan, decision X1). Beds and
 * admissions, the pharmacy store and the laboratory each change how a hospital
 * runs a whole department, so a site turns them on deliberately, and the Ward
 * Record profile and the demos never see them unless it does.
 *
 * The ids match server/config.py OPT_IN_MODULES exactly; the server publishes
 * them inside 'modules' on /api/health beside the others.
 *
 * The client default for these FAILS TOWARD HIDDEN, the opposite of the other
 * modules: before /api/health answers, when it cannot be reached and when it
 * answers non-OK, every opt-in module stays off. A tab for a module the server
 * has not confirmed would open onto routes that 404.
 */
export const OPT_IN_MODULE_IDS: readonly HospitalModuleId[] = ['adt', 'pharmacy-store', 'lab']

export function isOptInModule(id: HospitalModuleId): boolean {
  return (OPT_IN_MODULE_IDS as readonly string[]).includes(id)
}

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
  // The opt-in panels are built in later waves of the HIS plan. Until a file
  // exists its tab is not installed, whatever the flag says.
  adt: './pages/panels/BedBoardPanel.tsx',
  'pharmacy-store': './pages/panels/PharmacyStorePanel.tsx',
  lab: './pages/panels/LabPanel.tsx',
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
  './pages/panels/BedBoardPanel.tsx',
  './pages/panels/PharmacyStorePanel.tsx',
  './pages/panels/LabPanel.tsx',
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

/* ── Feature tabs ─────────────────────────────────────────────────────────────
 *
 * A feature tab is the same "a tab with no panel is not a tab" machinery as a
 * module tab, gated on a server FEATURE flag instead of a module flag. The
 * difference matters: a module is a back-office screen a site may or may not
 * run, while a feature may gate a clinical path, so features default OFF with
 * no client override and only the server can switch one on.
 *
 * OPD is the only feature tab. It goes on the doctor's rail (and the clinician
 * alias) and nobody else's; see DOCTOR_TABS. Its panel is built in a later wave
 * of the HIS plan, so until OpdPanel.tsx exists the tab is not installed.
 */
export type FeatureTabId = 'opd'

export const FEATURE_TAB_IDS: readonly FeatureTabId[] = ['opd']

/** Feature tab id → the panel file its builder must create (literal, because
 *  `import.meta.glob` is compile-time). */
export const FEATURE_TAB_PANEL_PATH: Record<FeatureTabId, string> = {
  opd: './pages/panels/OpdPanel.tsx',
}

const FEATURE_TAB_MODULES = import.meta.glob([
  './pages/panels/OpdPanel.tsx',
])

/** True for each feature tab whose panel file is present in THIS build. */
export const featureTabInstalled: Record<FeatureTabId, boolean> =
  Object.fromEntries(
    FEATURE_TAB_IDS.map(id => [id, FEATURE_TAB_PANEL_PATH[id] in FEATURE_TAB_MODULES]),
  ) as Record<FeatureTabId, boolean>

const FEATURE_TAB_PANELS: Record<FeatureTabId, LazyExoticComponent<ComponentType> | null> =
  Object.fromEntries(
    FEATURE_TAB_IDS.map(id => {
      const loader = FEATURE_TAB_MODULES[FEATURE_TAB_PANEL_PATH[id]]
      return [id, loader ? lazy(loader as () => Promise<{ default: ComponentType }>) : null]
    }),
  ) as Record<FeatureTabId, LazyExoticComponent<ComponentType> | null>

/** The panel to mount for a feature tab, or null if it is not a feature tab or
 *  its panel is not installed. The shell's one lookup for these. */
export function featurePanel(id: Tab): LazyExoticComponent<ComponentType> | null {
  return (FEATURE_TAB_PANELS as Record<string, LazyExoticComponent<ComponentType> | null>)[id] ?? null
}

function isFeatureTabId(id: Tab): id is FeatureTabId {
  return (FEATURE_TAB_IDS as readonly string[]).includes(id)
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
 *
 * EXCEPT THE OPT-IN MODULES (OPT_IN_MODULE_IDS), which default OFF here as they
 * do on the server, and stay off until /api/health says otherwise. The table is
 * DERIVED from the two lists so that a new module cannot be added to one and
 * forgotten in the other.
 */
export const DEFAULT_MODULE_FLAGS: Readonly<Record<HospitalModuleId, boolean>> =
  Object.fromEntries(
    HOSPITAL_MODULE_IDS.map(id => [id, !isOptInModule(id)]),
  ) as Record<HospitalModuleId, boolean>

/**
 * Build-time / runtime overrides, read the same way the landing-tab override is:
 *
 *   window.__ORB_MODULES__ = { procurement: false }   // index.html / ops
 *   VITE_ORB_MODULES=billing,payments                 // allow-list at build time
 *
 * The env form is an ALLOW-LIST (anything not named is off) because that is what
 * "this build ships these modules" means; the runtime object is a patch.
 *
 * An override may switch an OPT-IN module off but never on. Only the server
 * can confirm that an opt-in module's routes answer, so a build list or an
 * index.html patch naming 'adt' must not be able to put a Beds tab on the rail
 * that opens onto 404s.
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
  for (const id of OPT_IN_MODULE_IDS) {
    if (out[id] === true) delete out[id]
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

/** Subscribe to flag changes, `useSyncExternalStore`-shaped on purpose.
 *  One listener set serves the module flags, the feature flags and the health
 *  probe state, so a subscriber hears about any of the three. */
export function subscribeModuleFlags(listener: () => void): () => void {
  moduleFlagListeners.add(listener)
  return () => { moduleFlagListeners.delete(listener) }
}

/* ── Feature flags ────────────────────────────────────────────────────────────
 *
 * The HIS features the server publishes as 'features' on /api/health
 * (server/config.py FEATURE_FLAGS). Every one defaults OFF on the server and
 * here, and unlike module flags there is NO client override: a feature may gate
 * a clinical path, so only the server can switch one on. A key the server does
 * not publish stays off, and a key the server publishes that is not listed here
 * is ignored until a later wave names it.
 */
export type FeatureFlagId =
  | 'registration'
  | 'opd'
  | 'adt_strict_locations'
  | 'pharmacy_ward_stock'
  | 'ndps_register'
  | 'lab_analyser_listener'
  | 'result_quarantine'
  | 'result_corrections'
  | 'oru_order_link'
  | 'lab_critical_interfaced'
  | 'lab_portal_reports'
  | 'med_order_store'
  | 'med_order_lifecycle'

export const FEATURE_FLAG_IDS: readonly FeatureFlagId[] = [
  'registration', 'opd', 'adt_strict_locations', 'pharmacy_ward_stock', 'ndps_register',
  'lab_analyser_listener', 'result_quarantine', 'result_corrections', 'oru_order_link',
  'lab_critical_interfaced', 'lab_portal_reports', 'med_order_store', 'med_order_lifecycle',
]

export const DEFAULT_FEATURE_FLAGS: Readonly<Record<FeatureFlagId, boolean>> =
  Object.fromEntries(FEATURE_FLAG_IDS.map(id => [id, false])) as Record<FeatureFlagId, boolean>

/** Which feature flag each feature tab waits on. */
export const FEATURE_TAB_FLAG: Record<FeatureTabId, FeatureFlagId> = {
  opd: 'opd',
}

let featureFlags: Record<FeatureFlagId, boolean> = { ...DEFAULT_FEATURE_FLAGS }

/** The feature flags the client should act on right now. */
export function currentFeatureFlags(): Readonly<Record<FeatureFlagId, boolean>> {
  return featureFlags
}

/** Apply a partial update (from /api/health, or from a test) and notify. Only
 *  a strict boolean counts; anything else leaves the flag as it was. */
export function setFeatureFlags(next: Partial<Record<FeatureFlagId, boolean>>): void {
  const merged = { ...featureFlags }
  let changed = false
  for (const id of FEATURE_FLAG_IDS) {
    if (typeof next[id] === 'boolean' && merged[id] !== next[id]) {
      merged[id] = next[id] as boolean
      changed = true
    }
  }
  if (!changed) return
  featureFlags = merged
  for (const listener of moduleFlagListeners) listener()
}

/* ── The health probe's own state ─────────────────────────────────────────────
 *
 * 'pending' until the first answer, then 'answered' (a 200 with a JSON body) or
 * 'failed' (unreachable, non-OK, or not JSON). An account whose whole rail
 * waits on an opt-in module (a ward clerk, a lab technician) reads this to say
 * "checking" rather than "switched off" in the second before the server
 * answers, and "could not check" rather than "switched off" when it never does.
 */
export type HealthProbeState = 'pending' | 'answered' | 'failed'

let healthProbe: HealthProbeState = 'pending'

export function currentHealthProbe(): HealthProbeState {
  return healthProbe
}

function setHealthProbe(next: HealthProbeState): void {
  if (healthProbe === next) return
  healthProbe = next
  for (const listener of moduleFlagListeners) listener()
}

/** Reset the feature flags to all off and the probe to 'pending'. For tests
 *  and sign-out, beside resetModuleFlags. */
export function resetFeatureFlags(): void {
  featureFlags = { ...DEFAULT_FEATURE_FLAGS }
  healthProbe = 'pending'
  for (const listener of moduleFlagListeners) listener()
}

/* ── Clinical switches ────────────────────────────────────────────────────────
 *
 * The Orb Ward Record's switches, published as 'clinical' on /api/health
 * (server/main.py clinical_switches): ORB_SCORING_ENABLED, ORB_LENS_ENABLED,
 * ORB_SEPSIS_AUTOSTART, ORB_MODELS_ENABLED and ORB_WARD_RECORD. The first four
 * default TRUE and wardRecord defaults FALSE here until the probe answers, so a
 * site that never sets them sees exactly what it saw before. A key that is not
 * a strict boolean leaves the switch as it was. They share the module flag
 * listeners, so one subscription hears every answer of the probe.
 */
export interface ClinicalSwitches {
  scoringEnabled: boolean
  lensEnabled: boolean
  sepsisAutostart: boolean
  /** ORB_MODELS_ENABLED: false means no local model runs on this computer. */
  modelsEnabled: boolean
  /** ORB_WARD_RECORD: the Orb Ward Record profile. The server forces the four
   *  switches above off with it; the rail shows only the Ward Record screens. */
  wardRecord: boolean
}

export const DEFAULT_CLINICAL_SWITCHES: Readonly<ClinicalSwitches> = {
  scoringEnabled: true,
  lensEnabled: true,
  sepsisAutostart: true,
  modelsEnabled: true,
  wardRecord: false,
}

let clinicalSwitches: ClinicalSwitches = { ...DEFAULT_CLINICAL_SWITCHES }

/** The clinical switches the client should act on right now. */
export function currentClinicalSwitches(): Readonly<ClinicalSwitches> {
  return clinicalSwitches
}

/** Apply the server's 'clinical' answer (snake_case keys) and notify. */
export function setClinicalSwitches(next: {
  scoring_enabled?: unknown
  lens_enabled?: unknown
  sepsis_autostart?: unknown
  models_enabled?: unknown
  ward_record?: unknown
}): void {
  const merged = { ...clinicalSwitches }
  if (typeof next.scoring_enabled === 'boolean') merged.scoringEnabled = next.scoring_enabled
  if (typeof next.lens_enabled === 'boolean') merged.lensEnabled = next.lens_enabled
  if (typeof next.sepsis_autostart === 'boolean') merged.sepsisAutostart = next.sepsis_autostart
  if (typeof next.models_enabled === 'boolean') merged.modelsEnabled = next.models_enabled
  if (typeof next.ward_record === 'boolean') merged.wardRecord = next.ward_record
  if (merged.scoringEnabled === clinicalSwitches.scoringEnabled
      && merged.lensEnabled === clinicalSwitches.lensEnabled
      && merged.sepsisAutostart === clinicalSwitches.sepsisAutostart
      && merged.modelsEnabled === clinicalSwitches.modelsEnabled
      && merged.wardRecord === clinicalSwitches.wardRecord) return
  clinicalSwitches = merged
  for (const listener of moduleFlagListeners) listener()
}

/** Back to all on. For tests and sign-out. */
export function resetClinicalSwitches(): void {
  clinicalSwitches = { ...DEFAULT_CLINICAL_SWITCHES }
  for (const listener of moduleFlagListeners) listener()
}

/** The clinical switches as React state, re-rendering when the probe answers. */
export function useClinicalSwitches(): Readonly<ClinicalSwitches> {
  return useSyncExternalStore(subscribeModuleFlags, currentClinicalSwitches)
}

/**
 * Ask the server which modules this deployment runs.
 *
 * Unauthenticated (/api/health carries no PHI) so it can run before login, which
 * is what lets the rail be correct on first paint rather than flickering a tab
 * away a second after the user sees it. NEVER THROWS: a health endpoint that is
 * unreachable leaves the defaults in place, because a network blip must not be
 * able to remove a hospital's billing screen.
 *
 * The same answer carries the HIS feature flags ('features'), adopted here too,
 * so one probe settles the whole rail. A failed or non-OK probe leaves every
 * opt-in module and every feature OFF, which is their default.
 *
 * The module overrides (readModuleOverrides) are re-applied on top of the
 * server's answer, so an override that switches a module off still holds
 * after the probe answers.
 */
export async function refreshModuleFlags(
  fetchImpl: typeof fetch | undefined = typeof fetch === 'function' ? fetch : undefined,
  base = '/api',
): Promise<Readonly<Record<HospitalModuleId, boolean>>> {
  if (!fetchImpl) {
    setHealthProbe('failed')
    return currentModuleFlags()
  }
  try {
    const resp = await fetchImpl(`${base}/health`, { headers: { Accept: 'application/json' } })
    if (!resp.ok) {
      setHealthProbe('failed')
      return currentModuleFlags()
    }
    const body = await resp.json() as {
      modules?: Partial<Record<HospitalModuleId, boolean>>
      features?: Partial<Record<FeatureFlagId, boolean>>
      clinical?: {
        scoring_enabled?: unknown, lens_enabled?: unknown, sepsis_autostart?: unknown,
        models_enabled?: unknown, ward_record?: unknown,
      }
    }
    if (body?.modules && typeof body.modules === 'object') {
      /* The overrides are patched OVER the server's answer, not under it: a
         site that hides a module by build list or by window.__ORB_MODULES__
         must not get it back when the probe answers. readModuleOverrides has
         already dropped any attempt to switch an opt-in module on, so the
         server alone can still do that. */
      setModuleFlags({ ...body.modules, ...readModuleOverrides() })
      if (body.features && typeof body.features === 'object') setFeatureFlags(body.features)
      if (body.clinical && typeof body.clinical === 'object') setClinicalSwitches(body.clinical)
      setHealthProbe('answered')
    } else {
      setHealthProbe('failed')
    }
  } catch {
    /* Offline, server down, or a proxy returning HTML. Keep the defaults. */
    setHealthProbe('failed')
  }
  return currentModuleFlags()
}

/* ── Role features ────────────────────────────────────────────────────────────
 *
 * Each HIS role exists only while its feature is on (server/roles.py
 * ROLE_FEATURE, mirrored in src/lib/roles.ts). The feature is either an opt-in
 * module ('adt', 'lab') or a feature flag ('registration'). Both User
 * Management (which roles it offers) and the shell (whether the role's rail is
 * live or shows the switched-off notice) ask this one question.
 */
function roleFeatureOn(
  feature: RoleFeatureId,
  modules: Readonly<Record<HospitalModuleId, boolean>>,
  features: Readonly<Record<FeatureFlagId, boolean>>,
): boolean {
  if ((HOSPITAL_MODULE_IDS as readonly string[]).includes(feature)) {
    return modules[feature as HospitalModuleId] === true
  }
  return features[feature as FeatureFlagId] === true
}

/** True when `role` needs no feature, or its feature is switched on. */
export function isRoleFeatureOn(
  role: string | null | undefined,
  modules: Readonly<Record<HospitalModuleId, boolean>> = currentModuleFlags(),
  features: Readonly<Record<FeatureFlagId, boolean>> = currentFeatureFlags(),
): boolean {
  const feature = ROLE_FEATURE[normalizeRole(role)]
  return feature === undefined || roleFeatureOn(feature, modules, features)
}

/** True when User Management may create an account in `role` right now: its
 *  feature is on, or its ROLE_FEATURE_ALSO switch is (the ward clerk on an
 *  Orb Ward Record computer, with beds and admissions off). Mirrors the
 *  server's refusal in routes/admin_users_routes._require_role_switch. */
export function canStaffRole(
  role: string | null | undefined,
  modules: Readonly<Record<HospitalModuleId, boolean>> = currentModuleFlags(),
  features: Readonly<Record<FeatureFlagId, boolean>> = currentFeatureFlags(),
  clinical: Readonly<ClinicalSwitches> = currentClinicalSwitches(),
): boolean {
  if (isRoleFeatureOn(role, modules, features)) return true
  return ROLE_FEATURE_ALSO[normalizeRole(role)] === 'ward_record' && clinical.wardRecord
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
  {
    id: 'adt', label: 'Beds', pillar: 'Beds and admissions',
    icon: (
      // A made bed seen from the side: the bed itself is the unit this module
      // counts, assigns and moves patients between. Housekeeping's stripped bed
      // is the turnaround; this one is occupied or free.
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M2 4v16"/><path d="M2 8h18a2 2 0 0 1 2 2v10"/><path d="M2 17h20"/><path d="M6 8v9"/>
      </svg>
    ),
  },
  {
    id: 'pharmacy-store', label: 'Pharmacy store', pillar: 'Pharmacy store',
    icon: (
      // A store room with racked shelves: medicines held in stock, received and
      // counted. Deliberately not the flask Helix uses for dispensing.
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M22 8.35V20a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V8.35A2 2 0 0 1 3.26 6.5l8-3.2a2 2 0 0 1 1.48 0l8 3.2A2 2 0 0 1 22 8.35z"/>
        <path d="M6 18h12"/><path d="M6 14h12"/><path d="M6 10h12v12H6z"/>
      </svg>
    ),
  },
  {
    id: 'lab', label: 'Laboratory', pillar: 'Laboratory',
    icon: (
      // A specimen tube with a fill line: the sample is what the lab tracks
      // from collection to report.
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M14.5 2v17.5a2.5 2.5 0 0 1-5 0V2"/><path d="M8.5 2h7"/><path d="M14.5 16h-5"/>
      </svg>
    ),
  },
  {
    id: 'opd', label: 'OPD', pillar: 'Outpatients',
    icon: (
      // A stethoscope: the consultation itself, which is what the outpatient
      // clinic is for.
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M11 2v2"/><path d="M5 2v2"/>
        <path d="M5 3H4a2 2 0 0 0-2 2v4a6 6 0 0 0 12 0V5a2 2 0 0 0-2-2h-1"/>
        <path d="M8 15a6 6 0 0 0 12 0v-3"/><circle cx="20" cy="10" r="2"/>
      </svg>
    ),
  },
  {
    id: 'observations', label: 'Observations', pillar: 'Observations',
    icon: (
      // A clipboard with ruled lines: the paper observation sheet the clerk
      // copies from. Deliberately not a heart line, which would promise a
      // monitor and a score this screen never shows.
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V3h6v1"/>
        <path d="M8.5 9.5h7"/><path d="M8.5 13h7"/><path d="M8.5 16.5h4"/>
      </svg>
    ),
  },
]

const TAB_BY_ID = new Map(TABS.map(tab => [tab.id, tab]))

/**
 * PERMISSION TABS: a tab shown to an account that holds one permission,
 * whatever its role's opt-in module or feature says.
 *
 * Observations is the ward clerk's copy of the paper observation sheet (Orb
 * Ward Record, 9 October 2026). Her role exists for the beds module (ORB_ADT),
 * but copying the sheet does not need it: her patient list comes from
 * GET /api/nurse/transcribe/patients, which answers with the beds module off.
 * So this tab survives a switched-off role feature (getRoleConfig), and it
 * shows only while the account holds vitals.transcribe, the permission every
 * request it makes requires.
 */
export const PERMISSION_TABS: Readonly<Partial<Record<Tab, Permission>>> = {
  observations: VITALS_TRANSCRIBE,
}

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
 *
 * 'opd' (a feature tab, off by default) sits after the round: a doctor's
 * outpatient clinic is the other half of the day, and the tab appears only at a
 * site that has switched OPD on and in a build that carries its panel. It is in
 * DOCTOR_TABS rather than the doctor row so that a doctor in a surgery
 * department, who runs a clinic too, keeps it.
 */
const DOCTOR_TABS: readonly RoleTab[] = [
  t('today'), t('vigil'), t('round'), t('opd'), t('orders'), t('relay'), t('sage'), t('scribe'),
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
  /* The three opt-in modules (default OFF, see OPT_IN_MODULE_IDS). Admin is
   * the bed manager and the stores office, so admin gets all three once a site
   * switches them on and their panels are built. Beyond admin: the ward clerk
   * gets Beds, the pharmacist gets the Pharmacy store (the one pinned exception
   * to the money-adjacency rule, PHARMACY_DESK_MODULE_IDS) and the three lab
   * roles get the Laboratory. Doctors and nurses get none of them: they reach
   * lab work through Orders and the chart, and a nurse's read-only bed board
   * waits on ruling ADT-S7. */
  t('adt', 'hospital'),
  t('pharmacy-store', 'hospital'),
  t('lab', 'hospital'),
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

/**
 * The DIAGNOSTIC modules: the laboratory. Neither a money screen nor a
 * clinician's tab. Its rail belongs to the lab's own staff (technician,
 * pathologist, phlebotomist) and to admin; doctors and nurses order tests and
 * read results through Orders and the chart, never through this tab. It is
 * excluded from MONEY_MODULE_IDS because a lab technician holding it is not a
 * money screen reaching a non-admin role, and it is pinned by its own test so
 * that it cannot quietly reach a clinical rail either.
 */
export const DIAGNOSTIC_MODULE_IDS: readonly HospitalModuleId[] = ['lab']

/** The admin-only remainder: the money and back-office screens. */
export const MONEY_MODULE_IDS: readonly HospitalModuleId[] =
  HOSPITAL_MODULE_IDS.filter(id => !CLINICAL_MODULE_IDS.includes(id) && !DIAGNOSTIC_MODULE_IDS.includes(id))

/**
 * THE PHARMACY-DESK EXCEPTION (ruling GRN-R9, applied 3 October 2026).
 *
 * The Pharmacy store shows purchase values, so by this file's own rule it is a
 * money screen, and money screens never sit on a non-admin rail beside a
 * clinical action. Samuel's GRN-R9 ruling makes one exception: the pharmacist
 * gets the Pharmacy store tab. The pharmacist already holds procurement.write
 * through the API, receiving and counting drugs is the pharmacy's own job, and
 * the pharmacist's rail carries no dose administration.
 *
 * Pinned to exactly this one id and exactly the pharmacist by
 * shellConfig.test.ts, next to the dietary exception, so widening it is never a
 * quiet edit. It is NOT subtracted from MONEY_MODULE_IDS: the Pharmacy store
 * stays a money screen for every other role, and the money-adjacency test still
 * proves no doctor, nurse or surgeon ever sees it.
 */
export const PHARMACY_DESK_MODULE_IDS: readonly HospitalModuleId[] = ['pharmacy-store']

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
  /* 'pharmacy-store' after the verify queue, through the pinned
     PHARMACY_DESK_MODULE_IDS exception (GRN-R9). Off by default like every
     opt-in module, so a site that has not switched the store on sees exactly
     the rail it had. */
  pharmacist: {
    home: 'helix',
    tabs: [t('helix'), t('pharmacy-verify'), t('pharmacy-store', 'hospital'), t('sage')],
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
  /* ── The HIS roles (migration 48, users_role_batch) ─────────────────────────
     Each exists only while its feature is on (ROLE_FEATURE in src/lib/roles.ts,
     mirroring server/roles.py). While the feature is off the rail is empty and
     the shell shows the switched-off notice (getModuleOffNotice), never the
     unknown-role fallback of Patients and Messages, both of which would 403.
     None holds patient.write, so none is offered the admit action. */

  /* The registration desk (ruling OPD-N3): registration.read and .write, no
     money (that stays with the cashier) and no chart. Front Desk is the whole
     job. Needs the 'registration' feature. */
  receptionist: {
    home: 'frontdesk',
    tabs: [t('frontdesk', 'hospital')],
    primaryAction: null,
    floatingSage: false,
  },
  /* The ward clerk (adt-17): reads the bed board and moves patients between
     beds of the same department. No chart, no transfer between departments
     (ADT-S8), no leave or absconded (ADT-S7). Beds needs the 'adt' module.
     The Orb Ward Record (6 October 2026): she also copies observation rounds
     from the paper sheet. The Beds panel's Observations view
     (src/pages/panels/adt/ObservationsView.tsx) does that while the beds module
     is on; since 9 October 2026 she also has an Observations tab of her own
     (a PERMISSION_TABS entry), which works with the beds module off, so a Ward
     Record site does not have to switch beds on for her to key a round. */
  ward_clerk: {
    home: 'adt',
    tabs: [t('adt', 'hospital'), t('observations', 'clinical')],
    primaryAction: null,
    floatingSage: false,
  },
  /* The three lab roles (ruling LIS-R12): hospital-wide, no Relay, no Sage and
     no patient.read_all, so the Laboratory is the only screen. Needs the 'lab'
     module. */
  lab_technician: {
    home: 'lab',
    tabs: [t('lab', 'clinical')],
    primaryAction: null,
    floatingSage: false,
  },
  pathologist: {
    home: 'lab',
    tabs: [t('lab', 'clinical')],
    primaryAction: null,
    floatingSage: false,
  },
  phlebotomist: {
    home: 'lab',
    tabs: [t('lab', 'clinical')],
    primaryAction: null,
    floatingSage: false,
  },
}

/* ── The Orb Ward Record rail ──────────────────────────────────────────────
 *
 * With ORB_WARD_RECORD on (clinical.ward_record on /api/health) the rail shows
 * only what Orb Ward Record sells (SALES_PLAN_2026-10.md section 3, step 2):
 * the ward list and chart, with no scores (vigil); observation entry (the
 * nurse's shift); the clerk's own Observations tab, which works with ORB_ADT
 * off, and the beds, inside Beds, which still needs ORB_ADT; and Admin, which
 * then shows only the Ward Record
 * sections (Data, Users, the audit log, the Flight Recorder, the Trust
 * Center). Downtime is a page of its own, opened from the chart. Every other
 * tab is hidden, and a direct visit to one shows the plain "Not part of Orb
 * Ward Record" page instead (ClinicianShell).
 *
 * With only the models switched off (ORB_MODELS_ENABLED), the three model
 * tabs go: their every route answers 404 "Switched off on this computer".
 */
export const WARD_RECORD_TAB_IDS: readonly Tab[] = ['vigil', 'shift', 'adt', 'observations', 'admin']

/** The tabs that are a model, hidden while the models are switched off. */
export const MODEL_TAB_IDS: readonly Tab[] = ['sage', 'scribe', 'lens']

/** True when `id` is a screen these clinical switches take off the rail. */
export function hiddenByClinicalSwitches(
  id: Tab,
  clinical: Readonly<ClinicalSwitches> = currentClinicalSwitches(),
): boolean {
  if (clinical.wardRecord && !WARD_RECORD_TAB_IDS.includes(id)) return true
  return !clinical.modelsEnabled && MODEL_TAB_IDS.includes(id)
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
 *
 * `features` and `featureInstalled` are the same pair for feature tabs (OPD),
 * injectable for the same reason.
 */
export function getRoleConfig(
  role: string | null | undefined,
  department?: string,
  modules: Readonly<Record<HospitalModuleId, boolean>> = currentModuleFlags(),
  installed: Readonly<Record<HospitalModuleId, boolean>> = hospitalModuleInstalled,
  features: Readonly<Record<FeatureFlagId, boolean>> = currentFeatureFlags(),
  featureInstalled: Readonly<Record<FeatureTabId, boolean>> = featureTabInstalled,
  clinical: Readonly<ClinicalSwitches> = currentClinicalSwitches(),
): ShellRoleConfig {
  const key = normalizeRole(role)
  const base = ROLE_CONFIG[key] ?? FALLBACK_CONFIG
  const isSurgeon = department === 'Surgery' || department === 'General Surgery'
  /* A role that exists only while its feature is on (the HIS roles) has an
     EMPTY rail while that feature is off. The shell then shows the switched-off
     notice; the role never falls through to tabs it cannot open. */
  const roleLive = isRoleFeatureOn(key, modules, features)
  /* A PERMISSION tab (the clerk's Observations) needs no module or feature,
     so it survives a switched-off role feature, and it shows only while the
     account holds its permission. */
  const tabs = (!roleLive ? base.tabs.filter(entry => PERMISSION_TABS[entry.id] !== undefined)
    : isSurgeon && (key === 'doctor' || key === 'clinician')
      ? [...DOCTOR_TABS, t('surgical-suite')]
      : base.tabs
  )
    .filter(entry => {
      const needs = PERMISSION_TABS[entry.id]
      return needs === undefined || hasPermission(needs, key)
    })
    .filter(entry => entry.id !== 'today' || doctorHomeInstalled)
    /* A hospital-module tab survives only if its flag is on AND its panel is in
       the build. Either half missing means the tab is not there at all — never a
       tab that opens onto nothing, and never a tab for a module this deployment
       switched off. An OPT-IN module's flag must be explicitly true: a flag
       table that does not mention it (an older server, a partial fixture)
       leaves it off. */
    .filter(entry => !isHospitalModuleId(entry.id)
      || ((isOptInModule(entry.id) ? modules[entry.id] === true : modules[entry.id] !== false)
        && installed[entry.id]))
    /* A feature tab survives only if its feature flag is explicitly true AND its
       panel is in the build. */
    .filter(entry => !isFeatureTabId(entry.id)
      || (features[FEATURE_TAB_FLAG[entry.id]] === true && featureInstalled[entry.id] === true))
    /* The Orb Ward Record profile and the models switch (see above). */
    .filter(entry => !hiddenByClinicalSwitches(entry.id, clinical))
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
  features: Readonly<Record<FeatureFlagId, boolean>> = currentFeatureFlags(),
  featureInstalled: Readonly<Record<FeatureTabId, boolean>> = featureTabInstalled,
  clinical: Readonly<ClinicalSwitches> = currentClinicalSwitches(),
): Tab[] {
  return getRoleConfig(role, department, modules, installed, features, featureInstalled, clinical)
    .tabs.map(entry => entry.id)
}

/**
 * The tab a direct visit asked for, when the Orb Ward Record profile (or the
 * models switch) is what keeps it off this account's rail: the shell then
 * shows the "Not part of Orb Ward Record" page instead of ignoring the visit.
 * Null when the tab is on the rail, when the role would not have it anyway,
 * or when `requested` names no tab.
 */
export function switchedOffTab(
  role: string | null | undefined,
  department: string | undefined,
  requested: string | null | undefined,
  clinical: Readonly<ClinicalSwitches> = currentClinicalSwitches(),
): Tab | null {
  if (!requested || !TAB_BY_ID.has(requested as Tab)) return null
  const tab = requested as Tab
  if (!hiddenByClinicalSwitches(tab, clinical)) return null
  const unswitched = { ...clinical, wardRecord: false, modelsEnabled: true }
  const wouldHave = getAccessibleTabs(role, department, currentModuleFlags(), hospitalModuleInstalled,
    currentFeatureFlags(), featureTabInstalled, unswitched)
  return wouldHave.includes(tab) ? tab : null
}

/**
 * What a role whose whole rail waits on a switched-off feature should see.
 *
 * The HIS roles (receptionist, ward clerk, the three lab roles) exist only while
 * their feature is on. When the rail derived above is EMPTY for one of them, the
 * shell shows a plain notice instead of a blank screen or the unknown-role
 * fallback, and this says which one:
 *
 *   'checking'    the health probe has not answered yet;
 *   'unreachable' the probe failed, so Orb cannot say whether it is on;
 *   'off'         the server answered and the feature is off;
 *   'unavailable' the feature is on but the screen still cannot open (its panel
 *                 is not in this build, or a module it sits in is off).
 *
 * Null for every other role, and for a HIS role with at least one tab. Roles
 * that existed before (the cashier with Payments switched off) are untouched.
 */
export type ModuleOffState = 'checking' | 'unreachable' | 'off' | 'unavailable'

export interface ModuleOffNoticeInfo {
  readonly subject: RoleFeatureId
  readonly state: ModuleOffState
  /** The role's configured home, whose name and subtitle the page header shows
   *  while the notice stands, so the header never names a screen the account
   *  cannot open. */
  readonly home: Tab
}

export function getModuleOffNotice(
  role: string | null | undefined,
  department?: string,
  modules: Readonly<Record<HospitalModuleId, boolean>> = currentModuleFlags(),
  installed: Readonly<Record<HospitalModuleId, boolean>> = hospitalModuleInstalled,
  features: Readonly<Record<FeatureFlagId, boolean>> = currentFeatureFlags(),
  probe: HealthProbeState = currentHealthProbe(),
): ModuleOffNoticeInfo | null {
  const key = normalizeRole(role)
  const subject = ROLE_FEATURE[key]
  if (subject === undefined) return null
  if (getRoleConfig(key, department, modules, installed, features).tabs.length > 0) return null
  const home = (ROLE_CONFIG[key] ?? FALLBACK_CONFIG).home
  if (roleFeatureOn(subject, modules, features)) return { subject, state: 'unavailable', home }
  const state: ModuleOffState = probe === 'pending' ? 'checking' : probe === 'failed' ? 'unreachable' : 'off'
  return { subject, state, home }
}

/** False when the role's experience carries no floating Sage assistant (the
 *  HIS roles, which hold no sage.query). Every other role keeps it exactly as
 *  before. */
export function offersFloatingSage(
  role: string | null | undefined,
  clinical: Readonly<ClinicalSwitches> = currentClinicalSwitches(),
): boolean {
  // Sage is a model: no floating orb while the models (or the Ward Record
  // profile, which switches them off) say so.
  if (clinical.wardRecord || !clinical.modelsEnabled) return false
  return (ROLE_CONFIG[normalizeRole(role)] ?? FALLBACK_CONFIG).floatingSage !== false
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
