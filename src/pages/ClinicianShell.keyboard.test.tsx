/**
 * THE SHELL MUST NOT OWN THE KEYBOARD WHEN NOTHING IS OPEN.
 *
 * The shortcut sheet called useFocusTrap from a component that is always
 * mounted and renders nothing while closed. The trap armed at mount anyway, on
 * a null container, and stayed armed for the whole session: a capture-phase
 * document listener that cancelled every Tab (no focusable element in a null
 * container) and swallowed every Escape before any dialog could see it, plus a
 * body scroll lock. KeyboardShortcuts.test.tsx fires keys at `window`, which a
 * document capture listener never sees, so it stayed green through all of it.
 *
 * These tests mount the real shell (panels mocked, the keyboard layer real) and
 * dispatch keys at a focused element, the way a real keypress arrives.
 */
import { act, cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

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

vi.mock('../components/AdmitPatientModal', () => ({ default: () => null }))
vi.mock('../components/PulseWidget', () => ({ default: () => null }))
vi.mock('../components/ClockWidget', () => ({ default: () => null }))
vi.mock('../components/AiStatusChip', () => ({ default: () => null }))
vi.mock('../components/sage/SageFloatingAssistant', () => ({ default: () => null }))
vi.mock('../components/CommandPalette', () => ({ default: () => null }))
vi.mock('../components/AlertPermissionButton', () => ({ default: () => null }))
vi.mock('../components/MfaEnrollmentModal', () => ({ default: () => null }))
vi.mock('../services/criticalAlerts', () => ({ initCriticalAlerts: () => () => {} }))

// The landing panel carries two fields, so there is something to Tab between.
// Hoisted because vi.mock factories run before the module body.
const { twoFields, marker } = vi.hoisted(() => ({
  twoFields: () => ({
    default: () => (
      <div>
        <input aria-label="First field" />
        <input aria-label="Second field" />
      </div>
    ),
  }),
  marker: (tab: string) => ({ default: () => <div data-testid={`mounted-${tab}`}>{tab}</div> }),
}))

vi.mock('./panels/VigilPanel', twoFields)
vi.mock('./panels/RelayPanel', () => marker('relay'))
vi.mock('./panels/SagePanel', () => marker('sage'))
vi.mock('./panels/ForecastPanel', () => marker('forecast'))
vi.mock('./panels/AppointmentsPanel', () => marker('appointments'))
vi.mock('./panels/AdminPanel', twoFields)
vi.mock('../components/command/CommandCenter', twoFields)
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
import { HOSPITAL_MODULE_IDS, isOptInModule } from '../shellConfig'

async function renderShell() {
  const { container } = render(<ClinicianShell />)
  await waitFor(() => expect(container.querySelector('.shell__user-card')).toBeTruthy())
  return container
}

/** Dispatch a real, cancelable keydown at `target` and report what happened to it. */
function press(target: Element, key: string, init: KeyboardEventInit = {}) {
  const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init })
  target.dispatchEvent(event)
  return event
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, json: async () => [], headers: { get: () => '0' },
  })))
})

afterEach(() => {
  cleanup()
  document.body.style.overflow = ''
})

describe('with no dialog open, the shell leaves the keyboard alone', () => {
  it('lets Tab through to the browser so focus can move between fields', async () => {
    const container = await renderShell()
    const first = await waitFor(() => {
      const el = container.querySelector<HTMLInputElement>('input[aria-label="First field"]')
      expect(el).toBeTruthy()
      return el!
    })
    first.focus()
    expect(press(first, 'Tab').defaultPrevented).toBe(false)
    expect(press(first, 'Tab', { shiftKey: true }).defaultPrevented).toBe(false)
  })

  it('lets Escape reach an unrelated dialog listening on the document', async () => {
    const container = await renderShell()
    const unrelatedModalClose = vi.fn()
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') unrelatedModalClose() }
    document.addEventListener('keydown', onKey)
    try {
      press(container.querySelector('.shell__body')!, 'Escape')
      expect(unrelatedModalClose).toHaveBeenCalledTimes(1)
    } finally {
      document.removeEventListener('keydown', onKey)
    }
  })

  it('does not lock page scrolling', async () => {
    await renderShell()
    expect(document.body.style.overflow).not.toBe('hidden')
  })
})

describe('the shortcut sheet traps the keyboard only while it is open', () => {
  it('opens on ?, closes on Escape, and then releases Tab and Escape', async () => {
    const container = await renderShell()
    await act(async () => { press(container.querySelector('.shell__body')!, '?') })
    const sheet = document.querySelector('[role="dialog"][aria-labelledby="kbd-sheet-title"]')
    expect(sheet).toBeTruthy()
    expect(document.body.style.overflow).toBe('hidden')
    // Escape pressed inside the sheet closes it (the trap's own handler).
    await act(async () => { press(sheet!, 'Escape') })
    expect(document.querySelector('[aria-labelledby="kbd-sheet-title"]')).toBeNull()
    expect(document.body.style.overflow).not.toBe('hidden')

    const unrelated = vi.fn()
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') unrelated() }
    document.addEventListener('keydown', onKey)
    try {
      press(container.querySelector('.shell__body')!, 'Escape')
      expect(unrelated).toHaveBeenCalledTimes(1)
    } finally {
      document.removeEventListener('keydown', onKey)
    }
  })
})

/* The opt-in modules (Beds, Pharmacy store, Laboratory) default OFF since
   3 October 2026 (HIS build plan, decision X1), so they are on no rail here;
   they mount through the same loop and the same container once switched on. */
describe('every hospital module renders inside the shell scroll container', () => {
  it.each(HOSPITAL_MODULE_IDS.filter(id => !isOptInModule(id)))('"%s" mounts in .shell__module-scroll', async id => {
    const container = await renderShell()
    const button = container.querySelector<HTMLButtonElement>(`#nav-${id}`)
    expect(button).toBeTruthy()
    await act(async () => { button!.click() })
    await act(async () => { await Promise.resolve() })
    const panel = container.querySelector<HTMLElement>(`#panel-${id}`)
    expect(panel?.classList.contains('shell__module-scroll')).toBe(true)
    expect(panel?.style.display).toBe('flex')
  })
})
