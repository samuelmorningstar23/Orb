/**
 * The ward clerk's own Observations tab, rendered in the real shell
 * (Orb Ward Record, 9 October 2026).
 *
 * Her copy of the paper sheet used to live only inside the Beds panel, which
 * needs ORB_ADT on. With the beds module OFF she now gets an Observations tab
 * on her rail, it mounts the real WardObservationsPanel, and the panel lists
 * her patients from GET /api/nurse/transcribe/patients (never the bed board).
 */
import { render, screen, waitFor, act } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => ({
  user: { id: 'WC9', name: 'Wanda Clerk', role: 'ward_clerk', department: 'General Medicine' as string | undefined },
  getTranscribePatients: vi.fn(),
  apiFetch: vi.fn(),
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
  apiFetch: (...args: unknown[]) => h.apiFetch(...args),
}))

vi.mock('../api/nurse', () => ({
  getTranscribePatients: () => h.getTranscribePatients(),
  recordObservations: vi.fn(),
  previewNews2: vi.fn(),
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
vi.mock('./panels/BedBoardPanel.tsx', () => ({ default: () => <div data-testid="mounted-adt" /> }))

import ClinicianShell from './ClinicianShell'
import { DEFAULT_MODULE_FLAGS, resetFeatureFlags, resetModuleFlags } from '../shellConfig'

/** /api/health at a site with the beds module (ORB_ADT) switched off. */
const ADT_OFF_HEALTH = {
  modules: { ...DEFAULT_MODULE_FLAGS, adt: false } as Record<string, boolean>,
  features: { registration: false, opd: false },
}

function stubFetch() {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (String(url).endsWith('/health')) {
      return { ok: true, status: 200, json: async () => ADT_OFF_HEALTH }
    }
    return { ok: true, status: 200, json: async () => [], headers: { get: () => '0' } }
  }))
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
  localStorage.clear()
  localStorage.setItem('orb_user', JSON.stringify(h.user))
  h.apiFetch.mockReset()
  h.apiFetch.mockImplementation(() => new Promise(() => {}))
  h.getTranscribePatients.mockReset()
  h.getTranscribePatients.mockResolvedValue({
    department: 'General Medicine',
    patients: [
      { patient_id: 'P10', name: 'Meera Rao', mrn: 'UHID-10', ward: 'GM', bed: '10',
        admitted_at: '2026-10-07T08:50:00+00:00' },
      { patient_id: 'P2', name: 'Arjun Das', mrn: 'UHID-2', ward: 'GM', bed: '2', admitted_at: null },
    ],
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  resetModuleFlags()
  resetFeatureFlags()
})

describe('a ward clerk with the beds module switched off', () => {
  it('sees her Observations rail entry and her patients, without the bed board', async () => {
    stubFetch()
    const { container } = render(<ClinicianShell />)
    await settle()

    await waitFor(() => expect(railTabIds(container)).toEqual(['observations']))
    expect(container.querySelector('#nav-observations')!.getAttribute('aria-selected')).toBe('true')
    expect(container.querySelector('.module-off-notice')).toBeNull()

    // Her patients, Bed 2 before Bed 10, from her own list.
    expect(await screen.findByText('Arjun Das')).toBeTruthy()
    expect(screen.getByText('Meera Rao')).toBeTruthy()
    const names = Array.from(container.querySelectorAll('.wobs-row__name')).map(n => n.textContent)
    expect(names).toEqual(['Arjun Das', 'Meera Rao'])
    expect(screen.getAllByRole('button', { name: /^Copy from sheet for / })).toHaveLength(2)
    expect(h.getTranscribePatients).toHaveBeenCalledTimes(1)

    // Never the bed board, which answers only with the beds module on.
    const adtCalls = h.apiFetch.mock.calls.filter(([path]) => String(path).startsWith('/adt'))
    expect(adtCalls).toEqual([])
    expect(container.querySelector('[data-testid="mounted-adt"]')).toBeNull()
  })
})
