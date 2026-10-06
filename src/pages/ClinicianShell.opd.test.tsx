/**
 * The Outpatients tab in the real shell (opd-8).
 *
 * shellConfig.test.ts pins the table (OPD is on the doctor and clinician rails
 * only, after the round, with the feature on and its panel installed). This
 * renders the shell, because the Sage rule lives in the assembly: on the OPD
 * tab the floating Sage assistant is not rendered at all, so an outpatient on
 * screen can never become a Sage view_context patient. On every other tab the
 * doctor keeps it exactly as before.
 *
 * Panels are mocked to one marker each, as in the mount guard.
 */
import { act, fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => ({
  user: { id: 'D1', name: 'Dee Doctor', role: 'doctor', department: 'Cardiology' as string | undefined },
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

vi.mock('./panels/DoctorHomePanel', () => marker('today'))
vi.mock('./panels/VigilPanel', () => marker('vigil'))
vi.mock('./panels/RelayPanel', () => marker('relay'))
vi.mock('./panels/SagePanel', () => marker('sage'))
vi.mock('./panels/ForecastPanel', () => marker('forecast'))
vi.mock('./panels/AppointmentsPanel', () => marker('appointments'))
vi.mock('./panels/AdminPanel', () => marker('admin'))
vi.mock('./panels/RoundPanel', () => marker('round'))
vi.mock('./panels/ScribePanel', () => marker('scribe'))
vi.mock('./panels/LensPanel', () => marker('lens'))
vi.mock('./panels/OpdPanel.tsx', () => marker('opd'))
vi.mock('../components/orders/OrdersPanel', () => marker('orders'))
vi.mock('../components/command/CommandCenter', () => marker('command'))
vi.mock('../components/capacity/CapacitySimulator', () => marker('capacity'))
vi.mock('../components/coding/RevenueIntegrityPanel', () => marker('coding'))

import ClinicianShell from './ClinicianShell'
import { DEFAULT_MODULE_FLAGS, featureTabInstalled, resetFeatureFlags, resetModuleFlags } from '../shellConfig'

function stubFetch(features: Record<string, boolean>) {
  const spy = vi.fn(async (url: string) => {
    if (String(url).endsWith('/health')) {
      return { ok: true, status: 200, json: async () => ({ modules: { ...DEFAULT_MODULE_FLAGS }, features }) }
    }
    return { ok: true, status: 200, json: async () => [], headers: { get: () => '0' } }
  })
  vi.stubGlobal('fetch', spy)
  return spy
}

async function settle() {
  for (let i = 0; i < 4; i++) await act(async () => { await Promise.resolve() })
}

function railTabIds(container: HTMLElement): string[] {
  return Array.from(container.querySelectorAll('nav.shell__nav button[role="tab"]'))
    .map(b => b.id.replace(/^nav-/, ''))
}

beforeEach(() => {
  resetModuleFlags()
  resetFeatureFlags()
  h.user = { id: 'D1', name: 'Dee Doctor', role: 'doctor', department: 'Cardiology' }
})

afterEach(() => {
  vi.unstubAllGlobals()
  resetModuleFlags()
  resetFeatureFlags()
})

describe('the Outpatients tab hides the Sage assistant', () => {
  it('is installed now that OpdPanel.tsx exists', () => {
    expect(featureTabInstalled.opd).toBe(true)
  })

  it('a doctor with OPD on sees the tab; on it there is no Sage orb, elsewhere there is', async () => {
    stubFetch({ registration: true, opd: true })
    const { container } = render(<ClinicianShell />)
    await waitFor(() => expect(container.querySelector('.shell__user-card')).toBeTruthy())
    await settle()
    expect(railTabIds(container)).toContain('opd')
    expect(container.querySelector('[data-testid="sage-orb"]')).toBeTruthy()

    fireEvent.click(container.querySelector('#nav-opd') as HTMLElement)
    await waitFor(() => expect(container.querySelector('[data-testid="mounted-opd"]')).toBeTruthy())
    expect(container.querySelector('[data-testid="sage-orb"]')).toBeNull()

    fireEvent.click(container.querySelector('#nav-vigil') as HTMLElement)
    await waitFor(() => expect(container.querySelector('[data-testid="sage-orb"]')).toBeTruthy())
  })

  it('with OPD off the doctor has no Outpatients tab', async () => {
    stubFetch({ registration: true, opd: false })
    const { container } = render(<ClinicianShell />)
    await waitFor(() => expect(container.querySelector('.shell__user-card')).toBeTruthy())
    await settle()
    expect(railTabIds(container)).not.toContain('opd')
  })

  it.each(['nurse', 'admin', 'receptionist'])('a %s never gets the tab, OPD on', async role => {
    h.user = { id: 'U9', name: 'Other Role', role, department: role === 'nurse' ? 'Cardiology' : undefined }
    stubFetch({ registration: true, opd: true })
    const { container } = render(<ClinicianShell />)
    await waitFor(() => expect(container.querySelector('.shell__user-card')).toBeTruthy())
    await settle()
    expect(railTabIds(container)).not.toContain('opd')
  })
})
