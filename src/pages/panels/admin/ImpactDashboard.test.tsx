/**
 * Impact: an investor and CFO surface, so every figure must be one the model
 * can stand behind. Two units are never one number, an unmeasured stay is not
 * "0 days", simulated escalations are declared, and a recompute that fails or
 * cannot be sent says so beside the button.
 */
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const MODEL = {
  generated_at: '2026-10-03T09:00:00+00:00',
  currency: { code: 'INR', locale: 'en-IN' },
  period: { days: 30, start: '', end: '' },
  metrics: [
    { key: 'deterioration_caught_early', label: 'Deterioration caught early', value: 0.6,
      unit: 'ICU days avoided (est.)', amount: 9000, assumption: '4 NEWS2 critical escalations',
      evidence_count: 4 },
    { key: 'throughput', label: 'Throughput & length of stay', value: null,
      unit: 'days average length of stay', amount: 0,
      assumption: 'average length of stay not yet measured', evidence_count: 0 },
  ],
  totals: { monthly_savings: 9000, annual_savings: 108000, icu_days_avoided: 0.6, ades_avoided: 0.3 },
  simulated_excluded: 5,
  assumptions: {
    icu_day_cost_inr: { value: 15000, unit: '₹/ICU-day', citation: 'c' },
    deterioration_icu_avoidance_rate: { value: 0.15, unit: 'fraction', citation: 'c' },
  },
  has_data: true,
}

const apiFetch = vi.fn(async (path: string): Promise<unknown> => { void path; return MODEL })
vi.mock('../../../api/client', () => ({ apiFetch: (p: string) => apiFetch(p) }))

import ImpactDashboard from './ImpactDashboard'

beforeEach(() => { apiFetch.mockReset(); apiFetch.mockImplementation(async () => MODEL) })
afterEach(cleanup)

const openAssumptions = async () => {
  fireEvent.click(await screen.findByRole('button', { name: /Assumptions: tunable/ }))
}

describe('impact dashboard', () => {
  it('reports ICU days and adverse drug events as two figures, never one sum', async () => {
    render(<ImpactDashboard />)
    // Hero total plus the deterioration card's own unit.
    expect(await screen.findAllByText('ICU days avoided (est.)')).toHaveLength(2)
    expect(screen.getByText('adverse drug events avoided (est.)')).toBeTruthy()
    expect(screen.queryByText(/events prevented/)).toBeNull()
  })

  it('says length of stay is not yet measured rather than 0 days', async () => {
    render(<ImpactDashboard />)
    expect(await screen.findByText('Not yet measured')).toBeTruthy()
    expect(screen.queryByText(/^0$/)).toBeNull()
  })

  it('declares the escalations left out because simulated vitals drove them', async () => {
    render(<ImpactDashboard />)
    expect(await screen.findByText(/5 escalations driven by simulated vitals are left out/)).toBeTruthy()
  })

  it('refuses a rate above 1 with a reason, and caps the field at 1', async () => {
    render(<ImpactDashboard />)
    await openAssumptions()
    const rate = screen.getByLabelText('ICU-day avoidance rate') as HTMLInputElement
    expect(rate.max).toBe('1')
    fireEvent.change(rate, { target: { value: '15' } })
    expect(screen.getByRole('alert').textContent).toMatch(/fraction between 0 and 1/)
    expect((screen.getByRole('button', { name: /Apply & recompute/ }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('shows a failed recompute beside the button once a model is loaded', async () => {
    render(<ImpactDashboard />)
    await openAssumptions()
    apiFetch.mockImplementationOnce(async () => { throw new Error('Server unavailable') })
    fireEvent.change(screen.getByLabelText('ICU day cost'), { target: { value: '20000' } })
    fireEvent.click(screen.getByRole('button', { name: /Apply & recompute/ }))
    await waitFor(() => expect(screen.getByRole('alert').textContent)
      .toBe('Could not recompute: Server unavailable'))
  })
})
