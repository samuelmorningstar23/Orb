/**
 * NabhPanel — the four things this screen must never get wrong.
 *
 * 1. AN INDICATOR WITH NO NUMBER MUST PRINT WORDS. Not "0", not "0%", not "—",
 *    not an empty cell. This is a page a quality committee acts on and an
 *    assessor reads; a zero that means "we did not collect this" is the single
 *    most damaging thing the screen can render, because it looks like good news.
 *
 * 2. A DISABLED MODULE IS NOT A HOSPITAL WITH NO INCIDENTS. When a site keeps
 *    its quality register elsewhere the routes 404 with `module_disabled`. That
 *    must render as "not available at this site", never as an empty register —
 *    an empty register is a statement about the hospital.
 *
 * 3. THE ANONYMITY STATEMENT IS ON SCREEN BEFORE THE NETWORK ANSWERS, and it
 *    admits what it cannot guarantee. A person deciding whether to report a
 *    colleague's error must not be told a promise the platform cannot keep, and
 *    the sentence that says so cannot be one that depends on a fetch succeeding.
 *
 * 4. AN ACCOUNT THAT MAY REPORT BUT MAY NOT READ still gets the report form. A
 *    403 on the register must not blank the one surface every nurse needs.
 *
 * PROVEN ABLE TO FAIL (2026-08-12): each assertion was watched go red with its
 * defect reintroduced — `indicatorResult` changed to fall back to
 * `indicator.value ?? 0`, the `isModuleDisabled` branch removed, the
 * compile-time `ANONYMITY_FALLBACK` replaced by the server-only value, and the
 * 403 branch made to blank the panel — then restored.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import NabhPanel from './NabhPanel'
import { NabhApiError, indicatorResult, type Indicator } from '../../api/nabh'

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

const SERVER_STATEMENT =
  'An anonymous report stores no reporter name, id or role — that is enforced '
  + 'by a database constraint. It is NOT untraceable: the platform audit trail '
  + 'records which account called which endpoint.'

const META = {
  incident_types: ['medication_error', 'patient_fall', 'other'],
  severities: ['near_miss', 'no_harm', 'minor', 'moderate', 'major', 'sentinel'],
  sentinel_severities: ['major', 'sentinel'],
  statuses: ['reported', 'under_investigation', 'actions_pending', 'closed', 'void'],
  transitions: {},
  action_types: ['corrective'],
  dispositions: ['routine', 'lama', 'death'],
  death_dispositions: ['death'],
  ama_dispositions: ['lama', 'dama'],
  indicators: [],
  not_collected_catalogue: [],
  anonymity_statement: SERVER_STATEMENT,
  attestation: 'attestation',
  thresholds: {
    escalation_ack_minutes: 15, readmission_days: 7,
    closure_target_days: 30, disposition_coverage_floor: 0.9,
  },
} as unknown as import('../../api/nabh').NabhMeta

function indicator(over: Partial<Indicator>): Indicator {
  return {
    id: 'inpatient_mortality',
    code: 'QI-OUT-01',
    label: 'Inpatient mortality',
    definition: 'deaths over recorded dispositions',
    unit: '%',
    state: 'reported',
    value: 12,
    display: '12.0%',
    numerator: 3,
    denominator: 25,
    numerator_label: 'deaths',
    denominator_label: 'recorded dispositions',
    n_observed: 25,
    n_required: 20,
    direction: 'lower_is_better',
    is_proxy: false,
    caveat: 'coverage-gated',
    sources: ['nabh_discharge_dispositions'],
    blockers: [],
    excluded: {},
    detail: {},
    ...over,
  }
}

function dashboard(indicators: Indicator[]) {
  return {
    period: { start: '', end: '', start_date: '2025-03-01', end_date: '2025-04-09' },
    ward: null,
    generated_at: '',
    indicators,
    summary: {
      total: indicators.length,
      reported: indicators.filter((i) => i.state === 'reported').length,
      insufficient_data: indicators.filter((i) => i.state === 'insufficient_data').length,
      not_collected: indicators.filter((i) => i.state === 'not_collected').length,
      not_computable: 0,
    },
    denominators: {
      inpatient_days: {
        inpatient_days: 200, patients: 5, open_stays: 5, assumed_ends: 0,
        method: 'admission to discharge, clipped',
      },
      dispositions: {
        recorded: 4, discharge_events_observed: 20, coverage: 0.2, coverage_floor: 0.9,
      },
    },
    register_usage: {
      total: 0, in_use: false, by_type: {}, by_severity: {}, near_miss_share: null,
    },
    not_collected_catalogue: [{
      code: 'QI-INF-01', label: 'Hand-hygiene compliance',
      reason: 'Orb captures no hand-hygiene observation audit.',
      needed: 'An observation round.',
    }],
    attestation: 'Orb is not accredited and does not accredit.',
  } as unknown as import('../../api/nabh').IndicatorDashboard
}

afterEach(() => { vi.clearAllMocks() })

/* ═══════════════════════════════════════════════════════════════════════════
   1. The honesty rule
   ═══════════════════════════════════════════════════════════════════════════ */

describe('a withheld indicator renders as words, never as a number', () => {
  it('prints "Insufficient data" for a coverage-gated mortality rate', async () => {
    vi.mocked(api.getMeta).mockResolvedValue(META)
    vi.mocked(api.getIndicators).mockResolvedValue(dashboard([
      indicator({
        state: 'insufficient_data', value: null, display: null,
        numerator: 1, denominator: 4, n_observed: 4,
        detail: {
          coverage: 0.2, withheld_value: 25,
          coverage_shortfall: 'Dispositions were recorded for 20% of discharges.',
        },
      }),
    ]))

    render(<NabhPanel />)
    const card = await screen.findByText('QI-OUT-01')
    const article = card.closest('article') as HTMLElement

    // THE RESULT LINE — the one a reader transcribes — carries no digit at all.
    const result = article.querySelector('.nabh-card__result') as HTMLElement
    expect(result.textContent).toBe('Insufficient data')
    expect(result.textContent).not.toMatch(/\d/)
    expect(result.className).toContain('nabh-card__result--absent')
    // The arithmetic the server withheld must not appear anywhere on the card.
    expect(article.textContent).not.toContain('25.0%')
    // The reason IS on the card, not in a console nobody reads — and it may
    // legitimately quote the coverage percentage, which is why the no-digit
    // rule is asserted on the result element and not on the whole card.
    expect(within(article).getByText(/recorded for 20% of discharges/)).toBeInTheDocument()
  })

  it('prints "Not collected" and never a zero for an unused register', async () => {
    vi.mocked(api.getMeta).mockResolvedValue(META)
    vi.mocked(api.getIndicators).mockResolvedValue(dashboard([
      indicator({
        id: 'medication_error_rate', code: 'QI-MED-01',
        label: 'Reported medication errors per 1,000 inpatient-days',
        state: 'not_collected', value: null, display: null,
        numerator: 0, denominator: 200, n_observed: 200,
        numerator_label: 'reported medication errors',
        denominator_label: 'inpatient-days',
        blockers: ['The incident register recorded nothing in this period.'],
      }),
    ]))

    render(<NabhPanel />)
    const article = (await screen.findByText('QI-MED-01')).closest('article') as HTMLElement

    expect(within(article).getByText('Not collected')).toBeInTheDocument()
    expect(within(article).getByText(/register recorded nothing/)).toBeInTheDocument()
    // A denominator line is only printed for a REPORTED indicator; a
    // "0 of 200 inpatient-days" line reads exactly like a zero rate.
    expect(article.textContent).not.toMatch(/0 reported medication errors of 200/)
  })

  it('treats a "reported" indicator with no display as not computable', () => {
    // Should be impossible — `_finalise` will not emit it — but "impossible" is
    // how a zero reaches an accreditation document.
    const result = indicatorResult(indicator({ state: 'reported', display: null, value: 0 }))
    expect(result.isNumber).toBe(false)
    expect(result.text).toBe('Not computable')
  })

  it('never returns an empty, dashed or zero string for any non-reported state', () => {
    for (const state of ['insufficient_data', 'not_collected', 'not_computable'] as const) {
      const result = indicatorResult(indicator({ state, value: null, display: null }))
      expect(result.isNumber).toBe(false)
      expect(result.text.trim().length).toBeGreaterThan(3)
      expect(result.text).not.toMatch(/^[-—–0.\s%]*$/)
    }
  })

  it('still prints a real measured value when there is one', () => {
    // The guard has to DISCRIMINATE. One that always said "insufficient data"
    // would pass every test above and make the dashboard useless.
    const result = indicatorResult(indicator({}))
    expect(result).toEqual({ text: '12.0%', isNumber: true, state: 'reported' })
  })
})

/* ═══════════════════════════════════════════════════════════════════════════
   2. A disabled module is not an empty hospital
   ═══════════════════════════════════════════════════════════════════════════ */

it('renders a disabled module as unavailable, not as a hospital with no data', async () => {
  const disabled = new NabhApiError(
    'The NABH accreditation evidence module is switched off for this deployment '
    + '(ORB_NABH=0).', 404, 'module_disabled')
  vi.mocked(api.getMeta).mockRejectedValue(disabled)
  vi.mocked(api.getIndicators).mockRejectedValue(disabled)

  render(<NabhPanel />)

  expect(await screen.findByText(/switched off for this deployment/)).toBeInTheDocument()
  // No indicator grid, no "0 reported" summary that could be read as a finding.
  expect(screen.queryByText(/reported$/)).not.toBeInTheDocument()
  expect(screen.queryByRole('tab')).not.toBeInTheDocument()
})

/* ═══════════════════════════════════════════════════════════════════════════
   3. The anonymity statement
   ═══════════════════════════════════════════════════════════════════════════ */

describe('the anonymity statement', () => {
  it('is on screen before any request resolves, and admits the residual', async () => {
    // Every call hangs: this is the state a person could be making a decision in.
    vi.mocked(api.getMeta).mockReturnValue(new Promise(() => {}) as never)
    vi.mocked(api.getIndicators).mockReturnValue(new Promise(() => {}) as never)

    render(<NabhPanel />)
    fireEvent.click(screen.getByRole('tab', { name: 'Report an incident' }))

    expect(screen.getByText(/stores no reporter name, id or role/)).toBeInTheDocument()
    expect(screen.getByText(/NOT untraceable/)).toBeInTheDocument()
    expect(screen.getByText(/audit trail/)).toBeInTheDocument()
  })

  it('prefers the server sentence once /meta answers', async () => {
    vi.mocked(api.getMeta).mockResolvedValue(META)
    vi.mocked(api.getIndicators).mockResolvedValue(dashboard([indicator({})]))

    render(<NabhPanel />)
    await screen.findByText('QI-OUT-01')
    fireEvent.click(screen.getByRole('tab', { name: 'Report an incident' }))

    await waitFor(() => {
      expect(screen.getByText(SERVER_STATEMENT)).toBeInTheDocument()
    })
  })
})

/* ═══════════════════════════════════════════════════════════════════════════
   4. Report-only accounts
   ═══════════════════════════════════════════════════════════════════════════ */

it('keeps the report form usable for an account that cannot read the register', async () => {
  vi.mocked(api.getMeta).mockResolvedValue(META)
  vi.mocked(api.getIndicators).mockRejectedValue(
    new NabhApiError('Reading the register needs the quality-desk role.', 403, 'forbidden'))
  vi.mocked(api.reportIncident).mockResolvedValue({
    incident: { reference: 'INC-2025-26-000004' } as never,
    anonymity_statement: SERVER_STATEMENT,
  })

  render(<NabhPanel />)
  // The refusal is stated (twice: as a banner and as the empty indicators view)
  // and it points at the tab that still works.
  await waitFor(() => {
    expect(screen.getAllByText(/quality-desk role/).length).toBeGreaterThan(0)
  })
  expect(screen.getByText(/You can still file a report from the/)).toBeInTheDocument()

  fireEvent.click(screen.getByRole('tab', { name: 'Report an incident' }))
  fireEvent.change(screen.getByPlaceholderText(/Two strengths of the same drug/),
                   { target: { value: 'Near miss at the trolley' } })
  fireEvent.change(document.querySelector('.nabh-textarea') as HTMLTextAreaElement,
                   { target: { value: 'Caught before it reached the patient.' } })
  fireEvent.click(screen.getByLabelText(/Report this anonymously/))
  fireEvent.click(screen.getByRole('button', { name: 'Submit report' }))

  await waitFor(() => {
    expect(api.reportIncident).toHaveBeenCalledWith(
      expect.objectContaining({ is_anonymous: true, title: 'Near miss at the trolley' }))
  })
  expect(await screen.findByText(/INC-2025-26-000004/)).toBeInTheDocument()
})

/* ═══════════════════════════════════════════════════════════════════════════
   5. The gap list is on the page, not in a settings drawer
   ═══════════════════════════════════════════════════════════════════════════ */

it('shows what Orb does not collect beside what it does', async () => {
  vi.mocked(api.getMeta).mockResolvedValue(META)
  vi.mocked(api.getIndicators).mockResolvedValue(dashboard([indicator({})]))

  render(<NabhPanel />)

  expect(await screen.findByText('What Orb does not collect')).toBeInTheDocument()
  expect(screen.getByText('Hand-hygiene compliance')).toBeInTheDocument()
  expect(screen.getByText(/Needed: An observation round/)).toBeInTheDocument()
  // And the denominator coverage that gates mortality is stated in the open.
  expect(screen.getByText(/refuse to print below/)).toBeInTheDocument()
})

/* ═══════════════════════════════════════════════════════════════════════════
   6. The ward filter is a list of the hospital's wards
   A free-text filter refetched on every keystroke, and 'gm-1' typed for a ward
   stored as 'GM-1' emptied the register without a word.
   ═══════════════════════════════════════════════════════════════════════════ */

it('filters by a ward chosen from the hospital list, fetching once per change', async () => {
  vi.mocked(api.getMeta).mockResolvedValue({ ...META, wards: ['CCU', 'GM-1'] })
  vi.mocked(api.getIndicators).mockResolvedValue(dashboard([indicator({})]))

  render(<NabhPanel />)
  const filter = (await screen.findByRole('option', { name: 'GM-1' })).closest('select')!
  expect(within(filter).getAllByRole('option').map((o) => o.textContent))
    .toEqual(['All wards', 'CCU', 'GM-1'])

  const before = vi.mocked(api.getIndicators).mock.calls.length
  fireEvent.change(filter, { target: { value: 'GM-1' } })
  await waitFor(() => {
    expect(vi.mocked(api.getIndicators).mock.calls.length).toBe(before + 1)
  })
  expect(vi.mocked(api.getIndicators)).toHaveBeenLastCalledWith(
    expect.objectContaining({ ward: 'GM-1' }))
})

it('reports against a ward from the same list', async () => {
  vi.mocked(api.getMeta).mockResolvedValue({ ...META, wards: ['CCU', 'GM-1'] })
  vi.mocked(api.getIndicators).mockResolvedValue(dashboard([]))
  vi.mocked(api.reportIncident).mockResolvedValue({
    incident: { reference: 'INC-2025-26-000009' } as never,
    anonymity_statement: SERVER_STATEMENT,
  })

  render(<NabhPanel />)
  fireEvent.click(await screen.findByRole('tab', { name: 'Report an incident' }))
  const form = document.querySelector('.nabh-form') as HTMLFormElement
  const wardSelect = await waitFor(() => {
    const el = within(form).getByRole('option', { name: 'Not stated' }).closest('select')
    expect(el).toBeTruthy()
    return el!
  })
  fireEvent.change(wardSelect, { target: { value: 'CCU' } })
  fireEvent.change(screen.getByPlaceholderText(/Two strengths of the same drug/),
                   { target: { value: 'Pump alarm silenced' } })
  fireEvent.change(document.querySelector('.nabh-textarea') as HTMLTextAreaElement,
                   { target: { value: 'Found on the round.' } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit report' }))
  await waitFor(() => {
    expect(api.reportIncident).toHaveBeenCalledWith(expect.objectContaining({ ward: 'CCU' }))
  })
})

it('defaults the period to the LOCAL day, not the UTC one, after midnight', async () => {
  // At 00:30 IST the UTC date is still yesterday. toISOString() made the
  // default "To" yesterday and left the night's incidents out of the window.
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date(2026, 9, 3, 0, 30))
  try {
    vi.mocked(api.getMeta).mockResolvedValue(META)
    vi.mocked(api.getIndicators).mockResolvedValue(dashboard([]))
    render(<NabhPanel />)
    await waitFor(() => expect(api.getIndicators).toHaveBeenCalled())
    const params = vi.mocked(api.getIndicators).mock.calls[0][0] as { period_end: string; period_start: string }
    expect(params.period_end).toBe('2026-10-03')
    expect(params.period_start).toBe('2026-07-05')
  } finally {
    vi.useRealTimers()
  }
})

/* ═══════════════════════════════════════════════════════════════════════════
   7. Gap review F105 / F155: zero counts carry no tone, an incident carries
      the time it happened, and a reporter is named rather than a login id.
   ═══════════════════════════════════════════════════════════════════════════ */

it('does not colour a zero "insufficient data" count amber', async () => {
  vi.mocked(api.getMeta).mockResolvedValue(META)
  vi.mocked(api.getIndicators).mockResolvedValue(dashboard([indicator({})]))
  render(<NabhPanel />)
  const zero = await screen.findByText('0 insufficient data')
  expect(zero.className).not.toMatch(/--warn/)
})

it('sends the time the reporter says it happened, as UTC', async () => {
  vi.mocked(api.getMeta).mockResolvedValue(META)
  vi.mocked(api.getIndicators).mockResolvedValue(dashboard([]))
  vi.mocked(api.reportIncident).mockResolvedValue({
    incident: { reference: 'INC-2025-26-000010' } as never,
    anonymity_statement: SERVER_STATEMENT,
  })
  render(<NabhPanel />)
  fireEvent.click(await screen.findByRole('tab', { name: 'Report an incident' }))
  fireEvent.change(screen.getByPlaceholderText(/Two strengths of the same drug/),
                   { target: { value: 'Fall in the bathroom' } })
  fireEvent.change(document.querySelector('.nabh-textarea') as HTMLTextAreaElement,
                   { target: { value: 'Found on the floor at 02:00.' } })
  fireEvent.change(screen.getByLabelText(/When it happened/), { target: { value: '2025-04-01T02:00' } })
  fireEvent.click(screen.getByRole('button', { name: 'Submit report' }))
  await waitFor(() => {
    expect(api.reportIncident).toHaveBeenCalledWith(expect.objectContaining({
      occurred_at: new Date('2025-04-01T02:00').toISOString(),
    }))
  })
})
