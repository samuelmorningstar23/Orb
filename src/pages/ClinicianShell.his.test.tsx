/**
 * THE HIS ROLES AND THE HEALTH PROBE, RENDERED.
 *
 * shellConfig.test.ts pins the table; this renders the shell, because a table
 * can be right while the assembly is wrong (see ClinicianShell.mount.test.tsx).
 * Five things are asserted against the real shell:
 *
 *   1. the shell asks /api/health once, and a default site's rail is the same
 *      before and after the answer;
 *   2. a ward clerk or lab account whose module is off sees a plain notice,
 *      never a blank body, never the Patients and Messages fallback;
 *   3. the notice never says "switched off" when the server could not be asked;
 *   4. a receptionist with registration on gets Front Desk and nothing else;
 *   5. none of these accounts gets a Sage orb, the admit button, or a header
 *      naming a screen it cannot open.
 *
 * Panels are mocked to one marker each, as in the mount guard.
 */
import { render, waitFor, act } from '@testing-library/react'
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

import ClinicianShell from './ClinicianShell'
import {
  DEFAULT_MODULE_FLAGS,
  getAccessibleTabs,
  hospitalModuleInstalled,
  resetFeatureFlags,
  resetModuleFlags,
} from '../shellConfig'

type Health = { modules?: Record<string, boolean>; features?: Record<string, boolean> } | 'down' | 'non-ok'

/** A fetch whose /api/health answers `health`, and whose every other call
 *  answers an empty list, as the other shell tests do. Returns the spy. */
function stubFetch(health: Health) {
  const spy = vi.fn(async (url: string) => {
    if (String(url).endsWith('/health')) {
      if (health === 'down') throw new Error('offline')
      if (health === 'non-ok') return { ok: false, status: 503, json: async () => ({}) }
      return { ok: true, status: 200, json: async () => health }
    }
    return { ok: true, status: 200, json: async () => [], headers: { get: () => '0' } }
  })
  vi.stubGlobal('fetch', spy)
  return spy
}

/** The server's answer at a default site after the HIS wave: every standard
 *  module on, every opt-in module and every feature off. */
const DEFAULT_HEALTH = {
  modules: { ...DEFAULT_MODULE_FLAGS } as Record<string, boolean>,
  features: { registration: false, opd: false },
}

async function renderShell() {
  const { container } = render(<ClinicianShell />)
  await waitFor(() => expect(container.querySelector('.shell__user-card')).toBeTruthy())
  return container
}

/** Let the probe's promise chain settle and React re-render. */
async function settle() {
  for (let i = 0; i < 4; i++) await act(async () => { await Promise.resolve() })
}

function railTabIds(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('nav.shell__nav button[role="tab"]'))
    .map(b => b.id.replace(/^nav-/, ''))
}

function notice(container: HTMLElement) {
  return container.querySelector<HTMLElement>('.module-off-notice')
}

beforeEach(() => {
  resetModuleFlags()
  resetFeatureFlags()
})

afterEach(() => {
  vi.unstubAllGlobals()
  resetModuleFlags()
  resetFeatureFlags()
})

describe('the shell asks /api/health once, and a default site does not move', () => {
  it.each(['doctor', 'nurse', 'admin', 'pharmacist', 'cashier'])('%s keeps the same rail after the answer', async role => {
    h.user = { id: 'u2', name: 'Older Role', role, department: role === 'doctor' || role === 'nurse' ? 'Cardiology' : undefined }
    const before = getAccessibleTabs(role, h.user.department)
    const spy = stubFetch(DEFAULT_HEALTH)
    const container = await renderShell()
    await settle()
    expect(spy.mock.calls.filter(([url]) => String(url).endsWith('/health'))).toHaveLength(1)
    expect(railTabIds(container)).toEqual(before)
    expect(notice(container)).toBeNull()
  })
})

describe('a ward clerk whose Beds module is off', () => {
  beforeEach(() => {
    h.user = { id: 'WC1', name: 'Wanda Clerk', role: 'ward_clerk', department: 'Cardiology' }
  })

  it('sees the switched-off notice once the server answers, and no tab at all', async () => {
    stubFetch(DEFAULT_HEALTH)
    const container = await renderShell()
    await settle()
    expect(railTabIds(container)).toEqual([])
    const n = notice(container)
    expect(n).toBeTruthy()
    expect(n!.dataset.subject).toBe('adt')
    expect(n!.dataset.state).toBe('off')
    expect(n!.textContent).toContain('moduleOff.off')
    // Never the unknown-role fallback, whose panels would refuse the account.
    expect(container.querySelector('[data-testid="mounted-vigil"]')).toBeNull()
    expect(container.querySelector('[data-testid="mounted-relay"]')).toBeNull()
  })

  it('never says off when the server could not be asked', async () => {
    stubFetch('down')
    const container = await renderShell()
    await settle()
    expect(notice(container)!.dataset.state).toBe('unreachable')
  })

  it('a non-OK answer is "could not check", not "off"', async () => {
    stubFetch('non-ok')
    const container = await renderShell()
    await settle()
    expect(notice(container)!.dataset.state).toBe('unreachable')
  })

  it('says the screen cannot open yet when Beds is on but its panel is not built', async () => {
    stubFetch({ ...DEFAULT_HEALTH, modules: { ...DEFAULT_HEALTH.modules, adt: true } })
    const container = await renderShell()
    await settle()
    if (hospitalModuleInstalled.adt) {
      // The day BedBoardPanel.tsx lands, Beds is the rail and the notice goes.
      expect(railTabIds(container)).toEqual(['adt'])
      expect(notice(container)).toBeNull()
    } else {
      expect(railTabIds(container)).toEqual([])
      expect(notice(container)!.dataset.state).toBe('unavailable')
    }
  })

  it('gets no Sage orb, no admit button, and a header naming Beds, not Patients', async () => {
    stubFetch(DEFAULT_HEALTH)
    const container = await renderShell()
    await settle()
    expect(container.querySelector('[data-testid="sage-orb"]')).toBeNull()
    expect(container.querySelector('#btn-admit-patient')).toBeNull()
    expect(container.querySelector('.shell__page-title')!.textContent).toContain('Beds and admissions')
    expect(container.querySelector('.shell__page-subtitle')!.textContent).toBe('subtitle.adt')
  })

  it('is labelled by its own role in the user chip', async () => {
    stubFetch(DEFAULT_HEALTH)
    const container = await renderShell()
    expect(container.querySelector('.shell__user-role')!.textContent).toBe('role.ward_clerk · Cardiology')
  })
})

describe('a lab account whose Laboratory module is off', () => {
  it.each(['lab_technician', 'pathologist', 'phlebotomist'])('%s sees the notice for the laboratory', async role => {
    h.user = { id: 'L1', name: 'Lab Staff', role, department: undefined }
    stubFetch(DEFAULT_HEALTH)
    const container = await renderShell()
    await settle()
    expect(railTabIds(container)).toEqual([])
    expect(notice(container)!.dataset.subject).toBe('lab')
    expect(notice(container)!.dataset.state).toBe('off')
    expect(container.querySelector('[data-testid="sage-orb"]')).toBeNull()
    expect(container.querySelector('.shell__user-role')!.textContent).toBe(`role.${role}`)
  })
})

describe('the receptionist', () => {
  beforeEach(() => {
    h.user = { id: 'R1', name: 'Rekha Desk', role: 'receptionist', department: undefined }
  })

  it('with registration on gets Front Desk alone, mounted, and no notice', async () => {
    stubFetch({ ...DEFAULT_HEALTH, features: { registration: true, opd: false } })
    const container = await renderShell()
    await settle()
    expect(railTabIds(container)).toEqual(['frontdesk'])
    expect(notice(container)).toBeNull()
    await waitFor(() => expect(container.querySelector('[data-testid="mounted-frontdesk"]')).toBeTruthy())
    expect(container.querySelector('#btn-admit-patient')).toBeNull()
    expect(container.querySelector('[data-testid="sage-orb"]')).toBeNull()
  })

  it('with registration off sees the registration notice', async () => {
    stubFetch(DEFAULT_HEALTH)
    const container = await renderShell()
    await settle()
    expect(railTabIds(container)).toEqual([])
    expect(notice(container)!.dataset.subject).toBe('registration')
    expect(notice(container)!.dataset.state).toBe('off')
    expect(container.querySelector('[data-testid="mounted-frontdesk"]')).toBeNull()
  })
})
