/**
 * "Projected occupancy vs capacity" draws the capacity line.
 *
 * The Y axis was fitted to occupancy alone, so whenever the ward stayed under
 * capacity the capacity value fell outside it and Recharts (whose default for
 * an out-of-domain ReferenceLine is to discard it) drew nothing: the one line
 * the chart is titled after was missing on exactly the calm days. Recharts
 * needs real layout to render, so its parts are stubbed and the props the
 * simulator hands the line are asserted.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { ReactNode } from 'react'

vi.mock('../SizedChart', () => ({ default: ({ children }: { children: ReactNode }) => <>{children}</> }))
vi.mock('recharts', () => {
  const Pass = ({ children }: { children?: ReactNode }) => <div>{children}</div>
  const Nothing = () => null
  return {
    ResponsiveContainer: Pass,
    AreaChart: Pass,
    Area: Nothing, XAxis: Nothing, YAxis: Nothing, CartesianGrid: Nothing, Tooltip: Nothing,
    ReferenceLine: (p: { y: number; ifOverflow?: string }) => (
      <div data-testid="capacity-line" data-y={p.y} data-if-overflow={p.ifOverflow ?? 'discard'} />
    ),
  }
})
vi.mock('../../api/client', () => ({
  apiFetch: vi.fn(async () => ({
    baseline: { census: 20, capacity: 32, admission_rate: 4 },
    scenario: { effective_capacity: 32 },
    projection: [
      { step: 0, hour_offset: 0, projected_occupancy: 20, capacity: 32, overflow: 0, occupancy_pct: 62 },
      { step: 1, hour_offset: 12, projected_occupancy: 22, capacity: 32, overflow: 0, occupancy_pct: 69 },
    ],
    summary: {
      peak_occupancy: 22, peak_occupancy_pct: 69, peak_hour_offset: 12, hours_to_overflow: null,
      beds_short_at_peak: 0, suggested_discharges: 0, staffing_gap_nurses: 0,
      nurses_required_at_peak: 6, projected_admissions: 4, projected_discharges: 2,
    },
    assumptions: {},
  })),
}))

import CapacitySimulator from './CapacitySimulator'

describe('the What-If capacity line', () => {
  it('is drawn at the effective capacity even when occupancy stays below it', async () => {
    render(<CapacitySimulator />)
    const line = await waitFor(() => screen.getByTestId('capacity-line'), { timeout: 2000 })
    expect(line.getAttribute('data-y')).toBe('32')
    expect(line.getAttribute('data-if-overflow')).toBe('extendDomain')
  })
})
