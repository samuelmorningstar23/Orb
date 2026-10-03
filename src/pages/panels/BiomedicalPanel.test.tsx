/**
 * BiomedicalPanel — the five things this screen must never get wrong.
 *
 * 1. AN OVERDUE CALIBRATION IS A SENTENCE, NOT A NUMBER IN A TILE. A count that
 *    sits in a grid of five other counts is a count people stop seeing after a
 *    week. If anything is overdue, this panel must say so in words, above the
 *    tabs, whichever view the engineer left it on.
 *
 * 2. A DISABLED MODULE IS NOT AN EMPTY REGISTER. When a site keeps its
 *    equipment records elsewhere the routes 404 with `module_disabled`. That
 *    must render as "not available at this site" — never as an equipment list
 *    with nothing in it, because an empty equipment list is a statement about
 *    the hospital's machines, and a reassuring one.
 *
 * 3. `days_overdue === 0` IS OVERDUE. It is 0 on the FIRST overdue day, because
 *    the due date and today are the same day and nothing has elapsed.
 *    "Overdue by 0 days" reads like a rounding error and gets skipped, on the
 *    one row that most needs reading.
 *
 * 4. A DEVICE ORB READS NUMBERS FROM SAYS SO SPECIFICALLY. An overdue
 *    calibration on a monitor is not the same event as an overdue PM on a
 *    trolley, and a screen that renders them identically has thrown away the
 *    only reason this module is not just paperwork.
 *
 * 5. THE REASON THE REGISTER MATTERS SURVIVES A TOTAL NETWORK FAILURE. The
 *    sentence explaining that Orb ingests these numbers must be on screen in the
 *    first paint and still there when every request fails — which is exactly
 *    when a panel usually renders nothing.
 *
 * PROVEN ABLE TO FAIL (2026-08-12): each assertion was watched go red with its
 * defect reintroduced — the headline replaced by a tile-only count, the
 * `isModuleDisabled` branch removed, `overdueLabel` returning
 * `Overdue by ${days} days` unconditionally, the `measurement_at_risk` paragraph
 * deleted, and the PURPOSE_NOTE literal swapped for a server-only value — then
 * restored.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import BiomedicalPanel from './BiomedicalPanel'
import {
  BiomedicalApiError, dueLabel, formatDowntime, overdueLabel,
  type Asset, type BiomedicalMeta, type DueRegister, type Overview, type Schedule,
} from '../../api/biomedical'

vi.mock('../../api/biomedical', async () => {
  const actual = await vi.importActual<typeof import('../../api/biomedical')>(
    '../../api/biomedical',
  )
  return {
    ...actual,
    getMeta: vi.fn(),
    getOverview: vi.fn(),
    getDueRegister: vi.fn(),
    getCoverageGaps: vi.fn(),
    getAssets: vi.fn(),
    getServiceCalls: vi.fn(),
    getContracts: vi.fn(),
    saveAsset: vi.fn(),
  }
})

const api = await import('../../api/biomedical')

const META: BiomedicalMeta = {
  asset_categories: ['patient_monitor', 'ventilator'],
  criticalities: ['life_support', 'critical', 'routine'],
  asset_statuses: ['in_service', 'down'],
  unavailable_statuses: ['down'],
  schedule_types: ['calibration', 'preventive_maintenance'],
  schedule_type_labels: {
    calibration: 'Calibration',
    preventive_maintenance: 'Preventive maintenance',
  },
  schedule_results: ['pass', 'fail'],
  service_call_statuses: ['open', 'closed'],
  open_call_statuses: ['open'],
  service_call_severities: ['breakdown'],
  contract_types: ['amc'],
  contract_statuses: ['active'],
  default_lead_days: 30,
  default_notice_days: 60,
  due_rule: 'SERVER DUE RULE',
  contract_rule: 'SERVER CONTRACT RULE',
}

/** A schedule that fell due TODAY and has therefore just become overdue. */
const OVERDUE_TODAY: Schedule = {
  id: 1,
  asset_id: 10,
  schedule_type: 'calibration',
  schedule_type_label: 'Calibration',
  interval_days: 90,
  last_done_date: '2026-01-01',
  next_due_date: '2026-04-01',
  effective_due_date: '2026-04-01',
  grace_days: 0,
  lead_days: 30,
  responsible: null,
  last_certificate_reference: null,
  is_active: 1,
  never_done: false,
  is_overdue: true,
  days_overdue: 0,
  days_until_due: 0,
  due_soon: false,
  compliance: 'overdue',
  measurement_at_risk: true,
  asset_asset_tag: 'MON-014',
  asset_name: 'Bedside monitor',
  asset_ward: 'ICU',
  asset_criticality: 'life_support',
  asset_status: 'in_service',
  asset_produces_clinical_readings: 1,
}

const NEVER_DONE: Schedule = {
  ...OVERDUE_TODAY,
  id: 2,
  asset_id: 11,
  last_done_date: null,
  never_done: true,
  days_overdue: 152,
  measurement_at_risk: false,
  asset_asset_tag: 'PMP-003',
  asset_name: 'Infusion pump',
  asset_criticality: 'routine',
  asset_produces_clinical_readings: 0,
}

const EMPTY_OVERVIEW: Overview = {
  as_of: '2026-04-01',
  assets_total: 12,
  assets_down: 0,
  assets_under_maintenance: 0,
  life_support_assets: 3,
  measuring_assets: 6,
  overdue: [],
  overdue_count: 0,
  overdue_calibration_count: 0,
  overdue_life_support_count: 0,
  measurement_at_risk_count: 0,
  never_done_count: 0,
  due_soon: [],
  due_soon_count: 0,
  open_calls: [],
  open_call_count: 0,
  contracts_expiring: [],
  contracts_expiring_count: 0,
  contracts_expired_count: 0,
  assets_with_no_schedule: [],
  assets_with_no_schedule_count: 0,
  downtime_top: [],
}

const OVERDUE_OVERVIEW: Overview = {
  ...EMPTY_OVERVIEW,
  overdue: [OVERDUE_TODAY, NEVER_DONE],
  overdue_count: 2,
  overdue_calibration_count: 2,
  overdue_life_support_count: 1,
  measurement_at_risk_count: 1,
  never_done_count: 1,
}

const EMPTY_DUE: DueRegister = {
  as_of: '2026-04-01',
  overdue: [] as Schedule[], overdue_count: 0,
  due_soon: [] as Schedule[], due_soon_count: 0,
  overdue_calibration_count: 0, overdue_life_support_count: 0,
  never_done_count: 0, measurement_at_risk_count: 0,
}

function mockAll(overview: Overview = EMPTY_OVERVIEW, due: DueRegister = EMPTY_DUE) {
  vi.mocked(api.getMeta).mockResolvedValue(META as never)
  vi.mocked(api.getOverview).mockResolvedValue(overview as never)
  vi.mocked(api.getDueRegister).mockResolvedValue(due as never)
  vi.mocked(api.getCoverageGaps).mockResolvedValue({ assets: [], count: 0 } as never)
  vi.mocked(api.getAssets).mockResolvedValue({ assets: [], count: 0 } as never)
  vi.mocked(api.getServiceCalls).mockResolvedValue({ service_calls: [], count: 0 } as never)
  vi.mocked(api.getContracts).mockResolvedValue({ contracts: [], count: 0 } as never)
}

beforeEach(() => { vi.clearAllMocks() })
afterEach(() => { vi.restoreAllMocks() })

/* ═══════════════════════════════════════════════════════════════════════════
   1. An overdue calibration is a sentence, not a number in a tile
   ═══════════════════════════════════════════════════════════════════════════ */

describe('the overdue headline', () => {
  it('states in words that something is overdue, and names the risk', async () => {
    mockAll(OVERDUE_OVERVIEW)
    render(<BiomedicalPanel />)

    const headline = await screen.findByRole('status')
    expect(headline).toHaveTextContent(/2 schedules overdue/i)
    // Not just a count: the two facts that change what an engineer does first.
    expect(headline).toHaveTextContent(/1 on life-support equipment/i)
    expect(headline).toHaveTextContent(/Orb reads numbers from/i)
  })

  it('says so plainly when nothing is overdue, rather than showing nothing', async () => {
    mockAll(EMPTY_OVERVIEW)
    render(<BiomedicalPanel />)

    const headline = await screen.findByRole('status')
    expect(headline).toHaveTextContent(
      /No equipment is overdue for calibration or maintenance\./i)
  })
})

/* ═══════════════════════════════════════════════════════════════════════════
   2. A disabled module is not an empty register
   ═══════════════════════════════════════════════════════════════════════════ */

describe('when the site has switched the module off', () => {
  it('says the module is not available here, and never renders an empty register', async () => {
    vi.mocked(api.getMeta).mockRejectedValue(
      new BiomedicalApiError('off', 404, 'module_disabled'))
    vi.mocked(api.getOverview).mockRejectedValue(
      new BiomedicalApiError(
        'The biomedical equipment module is switched off for this deployment.',
        404, 'module_disabled'))
    vi.mocked(api.getDueRegister).mockRejectedValue(
      new BiomedicalApiError('off', 404, 'module_disabled'))
    vi.mocked(api.getCoverageGaps).mockRejectedValue(
      new BiomedicalApiError('off', 404, 'module_disabled'))

    render(<BiomedicalPanel />)

    expect(await screen.findByText(/Not available at this site/i)).toBeInTheDocument()
    expect(screen.getByText(/keeps its equipment and calibration register elsewhere/i))
      .toBeInTheDocument()
    // The dangerous wrong render: a register that looks empty.
    expect(screen.queryByText(/No equipment on the register yet/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})

/* ═══════════════════════════════════════════════════════════════════════════
   3. Day zero is overdue, and reads as such
   ═══════════════════════════════════════════════════════════════════════════ */

describe('the wording of a lapse', () => {
  it('reads "Overdue today" on the first overdue day, never "Overdue by 0 days"', () => {
    const label = overdueLabel({ is_overdue: true, days_overdue: 0 })
    expect(label).toBe('Overdue today')
    expect(label).not.toMatch(/0 days/)
  })

  it('counts up from the second day', () => {
    expect(overdueLabel({ is_overdue: true, days_overdue: 1 })).toBe('Overdue by 1 day')
    expect(overdueLabel({ is_overdue: true, days_overdue: 45 })).toBe('Overdue by 45 days')
  })

  it('never describes a compliant schedule as overdue', () => {
    expect(overdueLabel({ is_overdue: false, days_overdue: 0 })).toBe('')
    expect(dueLabel({ is_overdue: false, days_until_due: 0 })).toBe('Due today')
    expect(dueLabel({ is_overdue: false, days_until_due: 12 })).toBe('Due in 12 days')
    // …and never describes an overdue one as merely due.
    expect(dueLabel({ is_overdue: true, days_until_due: 0 })).toBe('')
  })

  it('renders the day-zero row as overdue on the screen', async () => {
    mockAll(OVERDUE_OVERVIEW)
    render(<BiomedicalPanel />)

    await waitFor(() => expect(screen.getByText('MON-014')).toBeInTheDocument())
    expect(screen.getByText('Overdue today')).toBeInTheDocument()
    expect(screen.queryByText(/Overdue by 0 days/)).not.toBeInTheDocument()
  })

  it('shows where a grace period ends, since overdue counts from there', async () => {
    const graced: Schedule = {
      ...OVERDUE_TODAY, next_due_date: '2026-03-25', effective_due_date: '2026-04-01', grace_days: 7,
    }
    mockAll({ ...OVERDUE_OVERVIEW, overdue: [graced], overdue_count: 1 })
    render(<BiomedicalPanel />)

    await waitFor(() => expect(screen.getByText('MON-014')).toBeInTheDocument())
    expect(screen.getByText(/Due 25 Mar 2026, grace to 1 Apr 2026/)).toBeInTheDocument()
  })
})

/* ═══════════════════════════════════════════════════════════════════════════
   4. A measuring device says so specifically
   ═══════════════════════════════════════════════════════════════════════════ */

describe('a device Orb reads numbers from', () => {
  it('gets the sentence about its readings; a device Orb does not read does not', async () => {
    mockAll(OVERDUE_OVERVIEW)
    render(<BiomedicalPanel />)

    await waitFor(() => expect(screen.getByText('MON-014')).toBeInTheDocument())

    const risk = screen.getAllByText(/Orb ingests readings from this device/i)
    expect(risk).toHaveLength(1)   // the monitor, not the pump

    // And the never-calibrated pump gets its own, different, statement.
    expect(screen.getByText(/Never recorded in Orb/i)).toBeInTheDocument()
  })
})

/* ═══════════════════════════════════════════════════════════════════════════
   5. The reason this register exists survives a dead network
   ═══════════════════════════════════════════════════════════════════════════ */

describe('the purpose note', () => {
  it('is on screen before any request resolves', () => {
    // Promises that never settle: the state a panel is in during the first paint.
    vi.mocked(api.getMeta).mockReturnValue(new Promise(() => {}) as never)
    vi.mocked(api.getOverview).mockReturnValue(new Promise(() => {}) as never)
    vi.mocked(api.getDueRegister).mockReturnValue(new Promise(() => {}) as never)
    vi.mocked(api.getCoverageGaps).mockReturnValue(new Promise(() => {}) as never)

    render(<BiomedicalPanel />)

    expect(screen.getByText(/keeps working and it reports the wrong figure/i))
      .toBeInTheDocument()
  })

  it('is still on screen when every request has failed', async () => {
    const boom = new Error('network down')
    vi.mocked(api.getMeta).mockRejectedValue(boom)
    vi.mocked(api.getOverview).mockRejectedValue(boom)
    vi.mocked(api.getDueRegister).mockRejectedValue(boom)
    vi.mocked(api.getCoverageGaps).mockRejectedValue(boom)

    render(<BiomedicalPanel />)

    await waitFor(() => expect(screen.getByText(/network down/i)).toBeInTheDocument())
    expect(screen.getByText(/keeps working and it reports the wrong figure/i))
      .toBeInTheDocument()
  })

  it('shows the boundary rule from the server once it has one', async () => {
    mockAll(EMPTY_OVERVIEW)
    render(<BiomedicalPanel />)
    expect(await screen.findAllByText('SERVER DUE RULE')).not.toHaveLength(0)
  })

  it('falls back to its own copy of the rule when the server has not answered', async () => {
    vi.mocked(api.getMeta).mockRejectedValue(new Error('no meta'))
    vi.mocked(api.getOverview).mockResolvedValue(EMPTY_OVERVIEW as never)
    vi.mocked(api.getDueRegister).mockResolvedValue(EMPTY_DUE as never)
    vi.mocked(api.getCoverageGaps).mockResolvedValue({ assets: [], count: 0 } as never)

    render(<BiomedicalPanel />)

    expect(await screen.findByText(/overdue ON its due date, not the day after/i))
      .toBeInTheDocument()
  })
})

/* ═══════════════════════════════════════════════════════════════════════════
   6. The coverage gap — a register that grades its own homework
   ═══════════════════════════════════════════════════════════════════════════ */

describe('equipment with no schedule at all', () => {
  it('is called out as a gap, not counted as compliant', async () => {
    mockAll(EMPTY_OVERVIEW)
    const gap: Asset = {
      id: 99, asset_tag: 'DEF-001', name: 'Defibrillator', category: 'defibrillator',
      manufacturer: null, model: null, serial_number: null, department: null,
      ward: 'ICU', location_detail: null, criticality: 'life_support',
      criticality_label: 'Life support', status: 'in_service',
      purchase_date: null, commissioned_date: null, purchase_cost_paise: null,
      cost_is_configured: 0, cost_display: 'Not configured',
      warranty_expiry_date: null, warranty_active: false,
      warranty_days_remaining: null, vendor_id: null,
      produces_clinical_readings: 1, notes: null, is_active: 1, is_available: true,
    }
    vi.mocked(api.getCoverageGaps).mockResolvedValue({ assets: [gap], count: 1 })

    render(<BiomedicalPanel />)

    expect(await screen.findByText('DEF-001')).toBeInTheDocument()
    expect(screen.getByText(/can never appear in the\s+overdue list/i)).toBeInTheDocument()
  })
})

/* ═══════════════════════════════════════════════════════════════════════════
   7. Downtime reads as a duration, and zero reads as words
   ═══════════════════════════════════════════════════════════════════════════ */

describe('formatDowntime', () => {
  it('reads zero as "None" rather than "0m"', () => {
    // "0m" in a downtime column is a measurement; "None" is the absence of one,
    // and the two get acted on differently in a vendor review.
    expect(formatDowntime(0)).toBe('None')
    expect(formatDowntime(null)).toBe('—')
    expect(formatDowntime(undefined)).toBe('—')
  })

  it('carries minutes up into hours and days', () => {
    expect(formatDowntime(45)).toBe('45m')
    expect(formatDowntime(60)).toBe('1h')
    expect(formatDowntime(125)).toBe('2h 5m')
    expect(formatDowntime(1440)).toBe('1d')
    expect(formatDowntime(1500)).toBe('1d 1h')
  })
})


/* ═══════════════════════════════════════════════════════════════════════════
   F098: Add equipment creates, it never overwrites
   An existing tag used to take the new name and lose its serial number.
   ═══════════════════════════════════════════════════════════════════════════ */

describe('adding a tag already on the register', () => {
  it('sends create_only and answers inside the dialog, keeping what was typed', async () => {
    mockAll()
    const sentence = 'PUMP-14 already exists as Infusion pump 14 on ward ICU. Nothing was changed.'
    vi.mocked(api.saveAsset).mockRejectedValue(
      new BiomedicalApiError(sentence, 409, 'already_exists'))
    render(<BiomedicalPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Add equipment' }))
    const dialog = await screen.findByRole('dialog', { name: 'Add equipment' })
    const inputs = dialog.querySelectorAll('input.bio-input')
    fireEvent.change(inputs[0], { target: { value: 'PUMP-14' } })
    fireEvent.change(inputs[1], { target: { value: 'New pump' } })
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Save equipment' }))
    })
    expect(api.saveAsset).toHaveBeenCalledWith(expect.objectContaining({
      asset_tag: 'PUMP-14', create_only: true }))
    expect(within(dialog).getByRole('alert')).toHaveTextContent(sentence)
    expect((inputs[0] as HTMLInputElement).value).toBe('PUMP-14')
  })
})
