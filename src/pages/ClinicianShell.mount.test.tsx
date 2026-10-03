/**
 * THE MOUNT GUARD — every tab the rail advertises must open onto a real panel.
 *
 * THE DEFECT THIS EXISTS FOR. All eleven hospital-module tabs shipped rendering
 * a BLANK page. shellConfig registered them (flag on, panel file present in the
 * build), the rail drew eleven buttons, the keyboard layer routed to them — and
 * ClinicianShell's `<section className="shell__body">` had no branch that
 * mounted any of them. Billing, Payments, Payer, Procurement, Front Desk,
 * Housekeeping, Workforce, Diet & Kitchen, Biomedical, NABH and ABDM each
 * opened onto an empty section.
 *
 * WHY THE SUITE WAS GREEN THROUGH IT. Two families of test existed and both
 * passed:
 *   • each panel's own unit test rendered THAT PANEL directly — it never asked
 *     whether the shell mounts it;
 *   • shellConfig.test.ts asserted which tab IDS a role is given — it never
 *     rendered the shell.
 * Neither side could see the seam between them, and the seam is where the whole
 * module surface lived. This is the class of defect where every part passes and
 * the assembly is broken; the only test that can catch it is one that spans
 * both halves.
 *
 * THE INVARIANT. For EVERY tab an admin's rail advertises — the rail read out
 * of the rendered DOM, not from a hand-written list — selecting that tab mounts
 * that tab's panel component. Not "a `<div id="panel-x">` exists"; the panel
 * COMPONENT renders. The empty-section failure produced a shell body with the
 * container absent entirely, and a future half-fix could equally produce the
 * container with nothing inside it, so both are asserted against.
 *
 * WHY THE PANELS ARE MOCKED. Each mock renders one marker node. That makes this
 * test a statement about the SHELL's wiring and nothing else — it deliberately
 * says nothing about whether a panel works, which is its own test's job. The
 * mocks also fail SAFE: if a mock ever stops intercepting, the real panel loads
 * and the marker is absent, so the test goes red rather than quietly passing.
 *
 * PROVEN TO FAIL: severing one id from ClinicianShell's mount loop
 * (`HOSPITAL_MODULE_IDS.filter(id => id !== 'billing').map(...)`) turns this
 * file red naming `billing`, on both the per-tab case and the whole-rail walk.
 */
import { render, waitFor, act } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

const ADMIN = { id: 'a1', name: 'Admin User', role: 'admin', department: undefined as string | undefined }

vi.mock('../api/client', () => ({
  getPatientDetail: vi.fn(async () => null),
  getAdministrations: vi.fn(async () => []),
  getTimelineProjection: vi.fn(() => new Promise(() => {})),
  getPatientForecast: vi.fn(() => new Promise(() => {})),
  getPatientAnalysis: vi.fn(() => new Promise(() => {})),
  dischargePatient: vi.fn(), addClinicalNote: vi.fn(), addMedication: vi.fn(),
  isSafetyAckRequired: () => false,
  getDischargeQrObjectUrl: vi.fn(), prefetchSageContext: vi.fn(async () => {}),
  getUnseenCounts: vi.fn(async () => ({})),
  administerMedication: vi.fn(),
  getUser: () => ADMIN,
  buildHeaders: () => ({}),
  socket: { on: vi.fn(), off: vi.fn(), emit: vi.fn() },
  getWardBrief: vi.fn(() => new Promise(() => {})),
  getSageAiPosture: vi.fn(() => new Promise(() => {})),
  apiFetch: vi.fn(() => new Promise(() => {})),
}))

vi.mock('../App', () => ({ useAuth: () => ({ user: ADMIN, logout: vi.fn() }) }))
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k, i18n: { language: 'en', changeLanguage: vi.fn(), on: vi.fn(), off: vi.fn() } }),
}))

// Shell chrome that is not a tab panel.
vi.mock('../components/AdmitPatientModal', () => ({ default: () => null }))
vi.mock('../components/PulseWidget', () => ({ default: () => null }))
vi.mock('../components/ClockWidget', () => ({ default: () => null }))
vi.mock('../components/AiStatusChip', () => ({ default: () => null }))
vi.mock('../components/sage/SageFloatingAssistant', () => ({ default: () => null }))
vi.mock('../components/CommandPalette', () => ({ default: () => null }))
vi.mock('../components/AlertPermissionButton', () => ({ default: () => null }))
vi.mock('../components/MfaEnrollmentModal', () => ({ default: () => null }))
vi.mock('../services/criticalAlerts', () => ({ initCriticalAlerts: () => () => {} }))

/**
 * One marker per panel. The marker carries the TAB ID the shell is supposed to
 * be mounting this component for, so a shell that mounts the wrong panel for a
 * tab fails just as loudly as one that mounts nothing.
 *
 * These paths are the shell's own module specifiers (the nine hand-written
 * lazy() branches) and shellConfig's `import.meta.glob` keys (the eleven module
 * panels) — that is why the two groups are written separately below.
 */
const marker = (tab: string) => ({
  default: () => <div data-testid={`mounted-${tab}`}>panel body for {tab}</div>,
})

// ── Hand-written lazy() branches in ClinicianShell ──────────────────────────
vi.mock('./panels/VigilPanel', () => marker('vigil'))
vi.mock('./panels/RelayPanel', () => marker('relay'))
vi.mock('./panels/SagePanel', () => marker('sage'))
vi.mock('./panels/ForecastPanel', () => marker('forecast'))
vi.mock('./panels/AppointmentsPanel', () => marker('appointments'))
vi.mock('./panels/AdminPanel', () => marker('admin'))
vi.mock('../components/command/CommandCenter', () => marker('command'))
vi.mock('../components/capacity/CapacitySimulator', () => marker('capacity'))
vi.mock('../components/coding/RevenueIntegrityPanel', () => marker('coding'))

// ── The eleven hospital-module panels, mounted through shellConfig's lookup ──
vi.mock('./panels/BillingPanel.tsx', () => marker('billing'))
vi.mock('./panels/PaymentsPanel.tsx', () => marker('payments'))
vi.mock('./panels/PayerPanel.tsx', () => marker('payer'))
vi.mock('./panels/ProcurementPanel.tsx', () => marker('procurement'))
vi.mock('./panels/FrontDeskPanel.tsx', () => marker('frontdesk'))
vi.mock('./panels/HousekeepingPanel.tsx', () => marker('housekeeping'))
vi.mock('./panels/WorkforcePanel.tsx', () => marker('workforce'))
vi.mock('./panels/DietaryPanel.tsx', () => marker('dietary'))
vi.mock('./panels/BiomedicalPanel.tsx', () => marker('biomedical'))
vi.mock('./panels/NabhPanel.tsx', () => marker('nabh'))
vi.mock('./panels/AbdmPanel.tsx', () => marker('abdm'))

import ClinicianShell from './ClinicianShell'
import {
  getAccessibleTabs, HOSPITAL_MODULE_IDS, OPT_IN_MODULE_IDS, hospitalModuleInstalled, currentModuleFlags, isOptInModule,
} from '../shellConfig'

/** The tabs shellConfig gives an administrator IN THIS BUILD. Derived, never
 *  listed: a twelfth module must be covered by this file the day it lands. */
const ADMIN_TABS = getAccessibleTabs('admin')

async function renderShell() {
  const { container } = render(<ClinicianShell />)
  await waitFor(() => expect(container.querySelector('.shell__user-card')).toBeTruthy())
  return container
}

/** The ids the RENDERED rail advertises — read from the DOM, because the rail
 *  is what a user can actually click, and that is the promise under test. */
function railTabIds(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('nav.shell__nav button[role="tab"]'))
    .map(b => b.id.replace(/^nav-/, ''))
}

/** Click a rail tab and wait for React to settle its lazy panel. */
async function selectTab(container: HTMLElement, id: string) {
  const button = container.querySelector<HTMLButtonElement>(`#nav-${id}`)
  expect(button, `the rail has no button for tab "${id}"`).toBeTruthy()
  await act(async () => { button!.click() })
  // Two ticks: one for the visitedTabs effect, one for the lazy import.
  await act(async () => { await Promise.resolve() })
}

/** What actually got mounted for `id`: the tabpanel container and its marker. */
function mountedPanel(container: HTMLElement, id: string) {
  const panel = container.querySelector<HTMLElement>(`#panel-${id}`)
  return {
    panel,
    marker: panel?.querySelector(`[data-testid="mounted-${id}"]`) ?? null,
    text: panel?.textContent?.trim() ?? '',
  }
}

describe('the shell mounts every tab its rail advertises', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200, json: async () => [], headers: { get: () => '0' },
    })))
  })

  /* Anchors the two ends of the seam to each other. If these ever disagree the
     per-tab cases below would silently stop covering whatever fell out. */
  it('the rendered rail advertises exactly the tabs shellConfig derives', async () => {
    const container = await renderShell()
    expect(railTabIds(container)).toEqual(ADMIN_TABS)
  })

  /* The specific surface that shipped blank. Stated separately from the walk so
     that a build which drops a module panel FILE (installed=false, so the tab is
     legitimately absent) fails here — naming the missing panel — instead of
     shrinking the walk to nothing and passing. */
  /* AMENDED 3 October 2026 (HIS build plan, decision X1): the eleven are the
     modules that default ON. The three opt-in modules (Beds, Pharmacy store,
     Laboratory) default OFF and are on no rail until the server switches them
     on; their panels are built in later waves, and the walk below covers each
     one the day it is both switched on and installed. */
  it('an admin rail advertises all eleven hospital modules', async () => {
    const standard = HOSPITAL_MODULE_IDS.filter(id => !isOptInModule(id))
    expect(standard).toHaveLength(11)
    expect(standard.filter(id => !hospitalModuleInstalled[id])).toEqual([])
    expect(standard.filter(id => !currentModuleFlags()[id])).toEqual([])
    const container = await renderShell()
    const rail = railTabIds(container)
    expect(standard.filter(id => !rail.includes(id))).toEqual([])
    expect(OPT_IN_MODULE_IDS.filter(id => rail.includes(id))).toEqual([])
  })

  /* One case per tab so a severed mount names the tab in the failing test's
     title — the diagnosis is the test name, not a stack trace. */
  it.each(ADMIN_TABS)('selecting "%s" mounts its panel, not an empty section', async id => {
    const container = await renderShell()
    await selectTab(container, id)

    const { panel, marker: mounted, text } = mountedPanel(container, id)
    expect(panel, `tab "${id}" is on the rail but the shell rendered no #panel-${id} container`).toBeTruthy()
    expect(mounted, `tab "${id}" opened onto an empty section — the shell mounted no panel component for it`).toBeTruthy()
    expect(text.length, `tab "${id}" mounted a panel that rendered nothing`).toBeGreaterThan(0)
    // The active tab's panel must also be the VISIBLE one, not mounted-hidden.
    // Clinical tabs render display:contents; hospital modules render inside
    // their own scroll container (display:flex, .shell__module-scroll).
    if ((HOSPITAL_MODULE_IDS as readonly string[]).includes(id)) {
      expect(panel!.style.display).toBe('flex')
      expect(panel!.classList.contains('shell__module-scroll')).toBe(true)
    } else {
      expect(panel!.style.display).toBe('contents')
    }
  })

  /* The same invariant walked in ONE session, the way a person uses the app —
     and reported as a list, so a broken loop names every tab it broke in a
     single run rather than one per re-run. */
  it('walking the whole rail leaves no tab showing an empty body', async () => {
    const container = await renderShell()
    const blank: string[] = []
    for (const id of railTabIds(container)) {
      await selectTab(container, id)
      const { marker: mounted } = mountedPanel(container, id)
      if (!mounted) blank.push(id)
      // Whatever the tab, the shell body is never left empty.
      expect(container.querySelector('.shell__body')?.textContent?.trim()).toBeTruthy()
    }
    expect(blank, 'these tabs are on the rail but mount no panel').toEqual([])
  })

  /* The guard's own guard: mounting is gated on the tab being ACCESSIBLE, so a
     panel must not appear for a tab this role was never given. Without this, a
     shell that mounted all panels unconditionally would pass everything above. */
  it('does not mount panels for tabs outside the role', async () => {
    const container = await renderShell()
    for (const id of railTabIds(container)) await selectTab(container, id)
    // A pharmacist-only surface an admin never gets.
    expect(ADMIN_TABS).not.toContain('pharmacy-verify')
    expect(container.querySelector('#panel-pharmacy-verify')).toBeNull()
  })
})
