/**
 * Sepsis Six card: progress is said ONCE, the chip is not green at 0 of 6,
 * the clock says what it measures, and a bundle left open for days reads as
 * not closed rather than as a breach happening now.
 */
import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const getPatientBundles = vi.fn()

vi.mock('../../api/bundles', () => ({
  getPatientBundles: (...a: unknown[]) => getPatientBundles(...a),
  startBundle: vi.fn(),
  completeBundleElement: vi.fn(),
  cancelBundle: vi.fn(),
}))

import CareBundleTimer from './CareBundleTimer'

const NOW = new Date('2026-10-03T10:00:00Z')

function bundle(over: Record<string, unknown> = {}) {
  return {
    id: 9,
    patient_id: 'P-1',
    bundle_type: 'sepsis6',
    status: 'active',
    // 3 minutes 7 seconds before NOW, stored as SQLite writes it (UTC, no zone).
    triggered_at: '2026-10-03 09:56:53',
    triggered_by: 'U1',
    trigger_reason: 'NEWS2 7',
    source: 'clinician',
    elements: [
      { key: 'oxygen', label: 'Give high-flow oxygen', target_min: 60, completed_at: null, state: 'pending' },
    ],
    completed_count: 0,
    total_count: 6,
    completion_pct: 0,
    compliance_pct: 0,
    on_track: true,
    ...over,
  }
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(NOW)
  getPatientBundles.mockReset()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('CareBundleTimer progress', () => {
  it('is neutral at 0 of 6 and states progress only once', async () => {
    getPatientBundles.mockResolvedValue([bundle()])
    render(<CareBundleTimer patientId="P-1" />)
    const chip = await screen.findByTestId('bundle-progress')
    expect(chip).toHaveAttribute('data-tone', 'neutral')
    expect(chip.textContent).toBe('0 of 6 done · within the 60-minute target')
    expect(screen.queryByText(/on track/)).toBeNull()
    expect(screen.queryByText(/% complete/)).toBeNull()
    expect(screen.queryByText('0/6 done')).toBeNull()
  })

  it('labels the clock as elapsed time against the 60-minute target', async () => {
    getPatientBundles.mockResolvedValue([bundle()])
    render(<CareBundleTimer patientId="P-1" />)
    const timer = await screen.findByRole('timer')
    expect(timer.textContent).toBe('Elapsed03:07of 60 min')
  })

  it('turns green only once an element is done on time', async () => {
    getPatientBundles.mockResolvedValue([bundle({ completed_count: 1, completion_pct: 17 })])
    render(<CareBundleTimer patientId="P-1" />)
    const chip = await screen.findByTestId('bundle-progress')
    expect(chip).toHaveAttribute('data-tone', 'ok')
    expect(chip.textContent).toBe('1 of 6 done · on track')
  })

  it('is amber past the target', async () => {
    getPatientBundles.mockResolvedValue([bundle({
      triggered_at: '2026-10-03 08:30:00', on_track: false,
    })])
    render(<CareBundleTimer patientId="P-1" />)
    const chip = await screen.findByTestId('bundle-progress')
    expect(chip).toHaveAttribute('data-tone', 'warn')
    expect(chip.textContent).toBe('0 of 6 done · past the 60-minute target')
  })

  it('says a days-old bundle was never closed instead of running a red clock', async () => {
    getPatientBundles.mockResolvedValue([bundle({
      triggered_at: '2026-09-24 08:00:00', on_track: false,
    })])
    render(<CareBundleTimer patientId="P-1" />)
    expect(await screen.findByText('Open 9 days, not closed')).toBeInTheDocument()
    const chip = screen.getByTestId('bundle-progress')
    expect(chip).toHaveAttribute('data-tone', 'neutral')
    expect(chip.textContent).toBe('0 of 6 done · not closed')
  })
})
