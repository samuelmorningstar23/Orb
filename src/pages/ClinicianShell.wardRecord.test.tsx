/**
 * The Orb Ward Record rail, rendered (6 October 2026).
 *
 * shellConfig.wardRecord.test.ts pins the table; this renders the shell with
 * /api/health answering clinical.ward_record true, because a table can be
 * right while the assembly is wrong. Asserted: each role's rail holds only
 * Ward Record screens; no Sage orb; a direct visit to a hidden screen (a
 * shortcut, another panel's link, or an address like /dashboard/orders)
 * shows the plain "Not part of Orb Ward Record" page and nothing else; a
 * role the profile leaves with no screen at all gets that page too; and an
 * ordinary site is unchanged.
 */
import { render, waitFor, act, fireEvent, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => ({
  user: { id: 'u1', name: 'Wanda Clerk', role: 'ward_clerk', department: 'Cardiology' as string | undefined },
}))

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
  getUser: () => h.user,
  buildHeaders: () => ({}),
  socket: { on: vi.fn(), off: vi.fn(), emit: vi.fn() },
  getWardBrief: vi.fn(() => new Promise(() => {})),
  getSageAiPosture: vi.fn(() => new Promise(() => {})),
  getVitalsFeed: vi.fn(() => new Promise(() => {})),
  apiFetch: vi.fn(() => new Promise(() => {})),
}))

vi.mock('../App', () => ({ useAuth: () => ({ user: h.user, logout: vi.fn() }) }))
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k, i18n: { language: 'en', changeLanguage: vi.fn(), on: vi.fn(), off: vi.fn() } }),
}))

vi.mock('../components/AdmitPatientModal', () => ({ default: () => null }))
vi.mock('../components/PulseWidget', () => ({ default: () => null }))
vi.mock('../components/ClockWidget', () => ({ default: () => null }))
vi.mock('../components/AiStatusChip', () => ({ default: () => null }))
vi.mock('../components/sage/SageFloatingAssistant', () => ({ default: () => <div data-testid="sage-orb" /> }))
vi.mock('../components/CommandPalette', () => ({ default: () => null }))
vi.mock('../components/AlertPermissionButton', () => ({ default: () => null }))
vi.mock('../components/MfaEnrollmentModal', () => ({ default: () => null }))
vi.mock('../services/criticalAlerts', () => ({ initCriticalAlerts: () => () => {} }))

const marker = (tab: string) => ({
  default: () => <div data-testid={`mounted-${tab}`}>panel body for {tab}</div>,
})

vi.mock('./panels/VigilPanel', () => marker('vigil'))
vi.mock('./panels/RelayPanel', () => marker('relay'))
vi.mock('./panels/SagePanel', () => marker('sage'))
vi.mock('./panels/ForecastPanel', () => marker('forecast'))
vi.mock('./panels/AppointmentsPanel', () => marker('appointments'))
vi.mock('./panels/AdminPanel', () => marker('admin'))
vi.mock('./panels/RoundPanel', () => marker('round'))
vi.mock('./panels/ScribePanel', () => marker('scribe'))
vi.mock('./panels/LensPanel', () => marker('lens'))
vi.mock('../components/orders/OrdersPanel', () => marker('orders'))
vi.mock('../components/command/CommandCenter', () => marker('command'))
vi.mock('../components/capacity/CapacitySimulator', () => marker('capacity'))
vi.mock('../components/coding/RevenueIntegrityPanel', () => marker('coding'))
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
// adt-4a (W4-B) installed the Beds panel; like every other module panel here
// it is a marker, so the rail is asserted without loading the real board.
vi.mock('./panels/BedBoardPanel.tsx', () => marker('adt'))
vi.mock('./panels/nurse/NursePanel', () => marker('shift'))
vi.mock('./panels/HelixPanel', () => marker('helix'))

import ClinicianShell from './ClinicianShell'
import {
  DEFAULT_MODULE_FLAGS,
  resetClinicalSwitches,
  resetFeatureFlags,
  resetModuleFlags,
} from '../shellConfig'

const WARD_RECORD_HEALTH = {
  modules: { ...DEFAULT_MODULE_FLAGS, adt: true } as Record<string, boolean>,
  features: {},
  clinical: {
    scoring_enabled: false, lens_enabled: false, sepsis_autostart: false,
    models_enabled: false, ward_record: true,
  },
}
const ORDINARY_HEALTH = { modules: { ...DEFAULT_MODULE_FLAGS } as Record<string, boolean>, features: {} }

function stubFetch(health: unknown) {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (String(url).endsWith('/health')) return { ok: true, status: 200, json: async () => health }
    return { ok: true, status: 200, json: async () => [], headers: { get: () => '0' } }
  }))
}

async function renderShell() {
  const { container } = render(<ClinicianShell />)
  await waitFor(() => expect(container.querySelector('.shell__user-card')).toBeTruthy())
  for (let i = 0; i < 4; i++) await act(async () => { await Promise.resolve() })
  return container
}

function rail(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('nav.shell__nav button[role="tab"]'))
    .map(b => b.id.replace(/^nav-/, ''))
}

const notPart = (c: HTMLElement) => c.querySelector<HTMLElement>('[data-testid="not-in-ward-record"]')

beforeEach(() => {
  resetModuleFlags()
  resetFeatureFlags()
  resetClinicalSwitches()
  window.history.pushState({}, '', '/dashboard')
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  resetModuleFlags()
  resetFeatureFlags()
  resetClinicalSwitches()
  window.history.pushState({}, '', '/')
})

describe('the rail under the Orb Ward Record profile', () => {
  it.each([
    ['doctor', 'General Medicine', ['vigil']],
    ['nurse', 'General Medicine', ['shift', 'vigil']],
    ['admin', undefined, ['vigil', 'adt', 'admin']],
    ['ward_clerk', 'General Medicine', ['adt']],
  ])('%s sees only the Ward Record screens', async (role, department, expected) => {
    h.user = { id: 'U1', name: 'Ward Person', role, department }
    stubFetch(WARD_RECORD_HEALTH)
    const container = await renderShell()
    await waitFor(() => expect(rail(container)).toEqual(expected))
    expect(container.querySelector('[data-testid="sage-orb"]')).toBeNull()
    expect(notPart(container)).toBeNull()
  })

  it('a pharmacist, left with no screen, gets the plain page and no blank body', async () => {
    h.user = { id: 'PH1', name: 'Pharma', role: 'pharmacist', department: undefined }
    stubFetch(WARD_RECORD_HEALTH)
    const container = await renderShell()
    await waitFor(() => expect(notPart(container)).toBeTruthy())
    expect(rail(container)).toEqual([])
    expect(notPart(container)!.textContent).toContain('Not part of Orb Ward Record')
  })
})

describe('a direct visit to a screen the profile hides', () => {
  beforeEach(() => {
    h.user = { id: 'D1', name: 'Dr Ward', role: 'doctor', department: 'General Medicine' }
  })

  it('a link to Sage shows the plain page, and the rail brings the ward list back', async () => {
    stubFetch(WARD_RECORD_HEALTH)
    const container = await renderShell()
    await waitFor(() => expect(rail(container)).toEqual(['vigil']))
    await act(async () => {
      window.dispatchEvent(new CustomEvent('orb:goto-tab', { detail: { tab: 'sage' } }))
    })
    const page = notPart(container)
    expect(page).toBeTruthy()
    expect(page!.textContent).toContain('Not part of Orb Ward Record')
    expect(page!.textContent).toContain('nav.sage is not used on this computer.')
    expect(container.querySelector('[data-testid="mounted-sage"]')).toBeNull()
    fireEvent.click(container.querySelector('#nav-vigil')!)
    expect(notPart(container)).toBeNull()
  })

  it('an address naming Orders shows the plain page', async () => {
    window.history.pushState({}, '', '/dashboard/orders')
    stubFetch(WARD_RECORD_HEALTH)
    const container = await renderShell()
    await waitFor(() => expect(notPart(container)).toBeTruthy())
    expect(container.querySelector('[data-testid="mounted-orders"]')).toBeNull()
  })

  it('on an ordinary site the same address and link change nothing', async () => {
    window.history.pushState({}, '', '/dashboard/orders')
    stubFetch(ORDINARY_HEALTH)
    const container = await renderShell()
    expect(rail(container)).toContain('sage')
    await act(async () => {
      window.dispatchEvent(new CustomEvent('orb:goto-tab', { detail: { tab: 'billing' } }))
    })
    expect(notPart(container)).toBeNull()
  })
})
