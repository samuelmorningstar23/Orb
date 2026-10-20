/**
 * NabhPanel with more than one facility of a hospital group (multisite-12).
 *
 * NABH accredits each hospital on its own. A group-wide reader gets a facility
 * filter for the indicators and the register, and a pack is always for one
 * facility: the pack buttons stay disabled until a facility is chosen. A
 * reader bound to one facility is told which facility the figures are for.
 * With one facility on record none of this is drawn.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import NabhPanel from './NabhPanel'

vi.mock('../../api/nabh', async () => {
  const actual = await vi.importActual<typeof import('../../api/nabh')>('../../api/nabh')
  return {
    ...actual,
    getMeta: vi.fn(),
    getIndicators: vi.fn(),
    getIncidents: vi.fn(),
    getIncident: vi.fn(),
    getExports: vi.fn(),
    reportIncident: vi.fn(),
    generatePack: vi.fn(),
  }
})

const api = await import('../../api/nabh')

const META = {
  incident_types: ['other'],
  severities: ['near_miss'],
  sentinel_severities: [],
  statuses: ['reported'],
  transitions: {},
  action_types: ['corrective'],
  dispositions: ['routine'],
  death_dispositions: [],
  ama_dispositions: [],
  indicators: [],
  not_collected_catalogue: [],
  anonymity_statement: 'statement',
  attestation: 'attestation',
  thresholds: {
    escalation_ack_minutes: 15, readmission_days: 7,
    closure_target_days: 30, disposition_coverage_floor: 0.9,
  },
} as unknown as import('../../api/nabh').NabhMeta

const NORTH = { id: 1, code: 'NTH', name: 'Northgate Hospital' }
const RIVER = { id: 2, code: 'RVS', name: 'Riverside Hospital' }

function dashboard(extra: Record<string, unknown> = {}) {
  return {
    period: { start: '', end: '', start_date: '2026-07-01', end_date: '2026-10-01' },
    ward: null,
    generated_at: '',
    indicators: [],
    summary: { total: 0, reported: 0, insufficient_data: 0, not_collected: 0, not_computable: 0 },
    denominators: {
      inpatient_days: {
        inpatient_days: 0, patients: 0, open_stays: 0, assumed_ends: 0, method: 'clipped',
      },
      dispositions: {
        recorded: 0, discharge_events_observed: 0, coverage: null, coverage_floor: 0.9,
      },
    },
    register_usage: { total: 0, in_use: false, by_type: {}, by_severity: {}, near_miss_share: null },
    not_collected_catalogue: [],
    attestation: 'attestation',
    ...extra,
  } as unknown as import('../../api/nabh').IndicatorDashboard
}

afterEach(() => { vi.clearAllMocks() })

describe('a group-wide reader chooses the facility', () => {
  it('offers the facilities and asks the server for the one chosen', async () => {
    vi.mocked(api.getMeta).mockResolvedValue(META)
    vi.mocked(api.getIndicators).mockResolvedValue(
      dashboard({ facility: null, facilities: [NORTH, RIVER] }))

    render(<NabhPanel />)
    const select = await screen.findByRole('combobox', { name: 'Facility' }) as HTMLSelectElement
    expect([...select.options].map((o) => o.textContent)).toEqual([
      'All facilities', 'Northgate Hospital (NTH)', 'Riverside Hospital (RVS)',
    ])
    // The first ask is for the whole group: no facility key at all.
    expect(vi.mocked(api.getIndicators).mock.calls[0][0]).not.toHaveProperty('facility')

    fireEvent.change(select, { target: { value: '2' } })
    await waitFor(() => {
      const last = vi.mocked(api.getIndicators).mock.calls.at(-1)?.[0]
      expect(last).toMatchObject({ facility: 2 })
    })
  })

  it('keeps the pack buttons off until a facility is chosen', async () => {
    vi.mocked(api.getMeta).mockResolvedValue(META)
    vi.mocked(api.getIndicators).mockResolvedValue(
      dashboard({ facility: null, facilities: [NORTH, RIVER] }))
    vi.mocked(api.getExports).mockResolvedValue({ exports: [] } as never)
    vi.mocked(api.generatePack).mockResolvedValue({
      reference: 'NABH-RVS-1',
      indicator_summary: { reported: 0, insufficient_data: 0, not_collected: 0 },
      register: { count: 0, anonymous_reports: 0 },
      content_sha256: 'abc',
      limitations: [],
    } as never)

    render(<NabhPanel />)
    const select = await screen.findByRole('combobox', { name: 'Facility' })
    fireEvent.click(screen.getByRole('tab', { name: 'Evidence pack' }))

    const generate = await screen.findByRole('button', { name: 'Generate pack' })
    expect(generate).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Download as a document' })).toBeDisabled()
    expect(screen.getByText(/Choose the facility the pack is for/)).toBeInTheDocument()

    fireEvent.change(select, { target: { value: '2' } })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Generate pack' })).toBeEnabled())
    expect(screen.getByRole('heading', { name: /at Riverside Hospital \(RVS\)$/ })).toBeInTheDocument()
    expect(screen.queryByText(/Choose the facility the pack is for/)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Generate pack' }))
    await waitFor(() => expect(api.generatePack).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.generatePack).mock.calls[0][0]).toMatchObject({ facility: 2 })
  })
})

describe('a reader bound to one facility', () => {
  it('is told which facility the figures are for and gets no filter', async () => {
    vi.mocked(api.getMeta).mockResolvedValue(META)
    vi.mocked(api.getIndicators).mockResolvedValue(
      dashboard({ facility: RIVER, facility_id: 2, facilities: [RIVER] }))

    render(<NabhPanel />)
    expect(await screen.findByText('Figures for Riverside Hospital (RVS).')).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'Facility' })).toBeNull()
  })
})

describe('one facility on record', () => {
  it('draws no facility filter and names no facility', async () => {
    vi.mocked(api.getMeta).mockResolvedValue(META)
    vi.mocked(api.getIndicators).mockResolvedValue(dashboard())

    render(<NabhPanel />)
    await waitFor(() => expect(api.getIndicators).toHaveBeenCalled())
    expect(screen.queryByRole('combobox', { name: 'Facility' })).toBeNull()
    expect(screen.queryByText(/^Figures for /)).toBeNull()
    expect(vi.mocked(api.getIndicators).mock.calls[0][0]).not.toHaveProperty('facility')
  })
})
