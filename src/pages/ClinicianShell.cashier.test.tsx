/**
 * A CASHIER SIGN-IN, RENDERED.
 *
 * The server staffs cashier accounts (server/roles.py STORABLE_ROLES, and
 * permissions.py "cashier": payments.read, .record, .close_counter). The shell
 * had no cashier row, so the account fell through to the unknown-role
 * fallback: a rail of Vigil and Relay, both of which 403 for it, landing on
 * Vigil, and no Payments tab anywhere. shellConfig.test.ts pins the table; this
 * renders the shell itself, because a table can be right while the assembly
 * still mounts the wrong thing (see ClinicianShell.mount.test.tsx).
 *
 * Panels are mocked to one marker each, as in the mount guard, so this is a
 * statement about the shell's wiring for this role and nothing else.
 */
import { render, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

const CASHIER = { id: 'CSH001', name: 'Chitra Cashier', role: 'cashier', department: undefined as string | undefined }

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
  getUser: () => CASHIER,
  buildHeaders: () => ({}),
  socket: { on: vi.fn(), off: vi.fn(), emit: vi.fn() },
  getWardBrief: vi.fn(() => new Promise(() => {})),
  getSageAiPosture: vi.fn(() => new Promise(() => {})),
  apiFetch: vi.fn(() => new Promise(() => {})),
}))

vi.mock('../App', () => ({ useAuth: () => ({ user: CASHIER, logout: vi.fn() }) }))
vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (k: string) => k, i18n: { language: 'en', changeLanguage: vi.fn(), on: vi.fn(), off: vi.fn() } }),
}))

vi.mock('../components/AdmitPatientModal', () => ({ default: () => null }))
vi.mock('../components/PulseWidget', () => ({ default: () => null }))
vi.mock('../components/ClockWidget', () => ({ default: () => null }))
vi.mock('../components/AiStatusChip', () => ({ default: () => null }))
vi.mock('../components/sage/SageFloatingAssistant', () => ({ default: () => null }))
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

describe('a cashier signing in', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true, status: 200, json: async () => [], headers: { get: () => '0' },
    })))
  })

  it('gets a rail of Payments alone, opens on it, and is offered no admit button', async () => {
    const { container } = render(<ClinicianShell />)
    await waitFor(() => expect(container.querySelector('.shell__user-card')).toBeTruthy())

    const rail = Array.from(container.querySelectorAll('nav.shell__nav button[role="tab"]'))
      .map(b => b.id.replace(/^nav-/, ''))
    expect(rail).toEqual(['payments'])

    await waitFor(() =>
      expect(container.querySelector('#panel-payments [data-testid="mounted-payments"]')).toBeTruthy())
    expect(container.querySelector('[data-testid="mounted-vigil"]')).toBeNull()
    expect(container.querySelector('[data-testid="mounted-relay"]')).toBeNull()
    expect(container.querySelector('#btn-admit-patient')).toBeNull()
  })
})
