/**
 * NO STARTUP FLASH (Samuel's ruling, 9 October 2026: fix the startup flash).
 *
 * The client used to assume every clinical switch was on until /api/health
 * answered, so a Ward Record computer showed the full rail, Sage and the score
 * widgets for a moment after sign-in or a reload. Now:
 *
 *   - the server writes the switches into the page (window.__ORB_SWITCHES__),
 *     and the very first render of the shell already uses them;
 *   - with no switches on the page and the probe still out, the shell draws a
 *     neutral frame: the Orb mark and "Starting Orb on this computer", no rail
 *     item, no panel;
 *   - a failed probe falls back to the switches this browser saved, and with
 *     nothing saved says the server could not be reached, never the full menu;
 *   - the probe's answer, when it comes, overrides the page.
 *
 * Every case loads shellConfig fresh (vi.resetModules), because the page's
 * switches are read once, at module load, exactly as in the browser.
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => ({
  user: { id: 'u1', name: 'Dev Doctor', role: 'doctor', department: 'Medicine' as string | undefined },
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
vi.mock('./panels/SurgicalSuitePanel', () => marker('surgical-suite'))
vi.mock('./panels/AdminPanel', () => marker('admin'))
vi.mock('./panels/RoundPanel', () => marker('round'))
vi.mock('./panels/ScribePanel', () => marker('scribe'))
vi.mock('./panels/LensPanel', () => marker('lens'))
vi.mock('./panels/DoctorHomePanel.tsx', () => marker('today'))
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
vi.mock('./panels/BedBoardPanel.tsx', () => marker('adt'))
vi.mock('./panels/nurse/NursePanel', () => marker('shift'))
vi.mock('./panels/HelixPanel', () => marker('helix'))

const SAVED_KEY = 'orb_computer_switches'

const WARD_RECORD = {
  modules: { billing: true, payments: true, adt: true },
  features: {},
  clinical: {
    scoring_enabled: false, lens_enabled: false, sepsis_autostart: false,
    models_enabled: false, ward_record: true,
  },
}
const ORDINARY = {
  modules: { billing: true, payments: true },
  features: {},
  clinical: {
    scoring_enabled: true, lens_enabled: true, sepsis_autostart: true,
    models_enabled: true, ward_record: false,
  },
}

/* Every screen Orb Ward Record does not sell, by rail id: Sage, Scribe, Lens,
   Messages (relay), Orders, Pharmacy (helix), Forecast, Simulator (capacity),
   Command, Revenue (coding), Appointments and the Surgical Suite. */
const NOT_WARD_RECORD = [
  'sage', 'scribe', 'lens', 'relay', 'orders', 'helix', 'forecast', 'capacity',
  'command', 'coding', 'appointments', 'surgical-suite',
]
const WARD_RECORD_RAIL = ['vigil', 'shift', 'adt', 'observations', 'admin']

/** The probe never answers. */
const pendingFetch = () => vi.fn(() => new Promise<Response>(() => {}))
/** The probe cannot reach the server. */
const downFetch = () => vi.fn(async () => { throw new TypeError('Failed to fetch') })
/** The probe answers with `health`. */
const answerFetch = (health: unknown) => vi.fn(async (url: string) => {
  if (String(url).endsWith('/health')) return { ok: true, status: 200, json: async () => health }
  return { ok: true, status: 200, json: async () => [], headers: { get: () => '0' } }
})

/** Load the shell as the browser would: page switches set before shellConfig loads. */
async function loadShell(page: unknown, fetchImpl: ReturnType<typeof vi.fn>) {
  vi.resetModules()
  if (page === undefined) delete window.__ORB_SWITCHES__
  else window.__ORB_SWITCHES__ = page
  vi.stubGlobal('fetch', fetchImpl)
  const shell = await import('./ClinicianShell')
  const config = await import('../shellConfig')
  return { ClinicianShell: shell.default, config }
}

function rail(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('nav.shell__nav button[role="tab"]'))
    .map(b => b.id.replace(/^nav-/, ''))
}

async function settle() {
  for (let i = 0; i < 4; i++) await act(async () => { await Promise.resolve() })
}

beforeEach(() => {
  localStorage.clear()
  h.user = { id: 'u1', name: 'Dev Doctor', role: 'doctor', department: 'Medicine' }
  window.history.pushState({}, '', '/dashboard')
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  delete window.__ORB_SWITCHES__
  localStorage.clear()
})

describe('with no switches on the page and the probe still out', () => {
  it('shows the neutral frame: the Orb mark and one line, no rail item, no panel, no Sage', async () => {
    const { ClinicianShell } = await loadShell(undefined, pendingFetch())
    const { container } = render(<ClinicianShell />)
    await settle()
    expect(screen.getByTestId('orb-startup-frame')).toBeTruthy()
    expect(screen.getByRole('status')).toHaveTextContent('Starting Orb on this computer')
    expect(container.querySelector('.orb-logo')).toBeTruthy()
    expect(rail(container)).toEqual([])
    expect(container.querySelector('.shell')).toBeNull()
    expect(container.querySelector('[data-testid^="mounted-"]')).toBeNull()
    expect(screen.queryByTestId('sage-orb')).toBeNull()
  })
})

describe('with the Ward Record switches on the page', () => {
  it.each([
    ['doctor', 'Medicine'],
    ['doctor', 'General Surgery'],
    ['nurse', 'Medicine'],
    ['admin', undefined],
  ])('the first render of a %s (%s) shows only the Ward Record rail', async (role, department) => {
    h.user = { id: 'u1', name: 'Someone', role, department }
    const { ClinicianShell } = await loadShell(WARD_RECORD, pendingFetch())
    // Asserted on the synchronous first render, before any promise settles.
    const { container } = render(<ClinicianShell />)
    const ids = rail(container)
    expect(ids.length).toBeGreaterThan(0)
    for (const id of ids) expect(WARD_RECORD_RAIL).toContain(id)
    for (const id of NOT_WARD_RECORD) expect(ids).not.toContain(id)
    expect(screen.queryByTestId('sage-orb')).toBeNull()
    expect(screen.queryByTestId('orb-startup-frame')).toBeNull()
  })

  it('the probe answering an ordinary site overrides the page', async () => {
    const { ClinicianShell } = await loadShell(WARD_RECORD, answerFetch(ORDINARY))
    const { container } = render(<ClinicianShell />)
    expect(rail(container)).not.toContain('sage')
    await settle()
    expect(rail(container)).toContain('sage')
    expect(rail(container)).toContain('orders')
  })
})

describe('when the probe fails', () => {
  it('falls back to the switches this browser saved', async () => {
    localStorage.setItem(SAVED_KEY, JSON.stringify(WARD_RECORD))
    const { ClinicianShell } = await loadShell(undefined, downFetch())
    const { container } = render(<ClinicianShell />)
    expect(rail(container)).toEqual([])
    await settle()
    const ids = rail(container)
    expect(ids.length).toBeGreaterThan(0)
    for (const id of ids) expect(WARD_RECORD_RAIL).toContain(id)
    expect(screen.queryByText(/Could not reach/)).toBeNull()
  })

  it('with nothing saved, says the server could not be reached and never shows the menu', async () => {
    const { ClinicianShell } = await loadShell(undefined, downFetch())
    const { container } = render(<ClinicianShell />)
    await settle()
    expect(screen.getByRole('alert')).toHaveTextContent("Could not reach this computer's server")
    expect(rail(container)).toEqual([])
    expect(container.querySelector('.shell')).toBeNull()

    // Try again: the neutral frame while it asks, then the shell once it answers.
    vi.stubGlobal('fetch', answerFetch(WARD_RECORD))
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await settle()
    expect(screen.queryByText(/Could not reach/)).toBeNull()
    const ids = rail(container)
    expect(ids.length).toBeGreaterThan(0)
    for (const id of ids) expect(WARD_RECORD_RAIL).toContain(id)
  })

  it('keeps the page switches and never falls to the full menu', async () => {
    const { ClinicianShell } = await loadShell(WARD_RECORD, downFetch())
    const { container } = render(<ClinicianShell />)
    await settle()
    for (const id of NOT_WARD_RECORD) expect(rail(container)).not.toContain(id)
    expect(screen.queryByText(/Could not reach/)).toBeNull()
  })
})

describe('the saved switches', () => {
  it('are written each time the switches become known, and survive into the next load', async () => {
    const first = await loadShell(undefined, answerFetch(WARD_RECORD))
    render(<first.ClinicianShell />)
    await settle()
    expect(JSON.parse(localStorage.getItem(SAVED_KEY) ?? 'null')?.clinical?.ward_record).toBe(true)
    cleanup()
    // A later load (another sign-in on this computer) with the server down.
    const second = await loadShell(undefined, downFetch())
    const { container } = render(<second.ClinicianShell />)
    await settle()
    for (const id of NOT_WARD_RECORD) expect(rail(container)).not.toContain(id)
    expect(rail(container).length).toBeGreaterThan(0)
  })
})
