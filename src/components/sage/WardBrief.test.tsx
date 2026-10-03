/**
 * Ward brief: a scoped brief (census null) names its scope instead of printing
 * "admitted hospital-wide" with no number, the footer claims narration only
 * when a narrative is shown, and socket bursts coalesce into one refetch (each
 * GET writes a PHI read row on the server).
 */
import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const getWardBrief = vi.fn()
const handlers: Record<string, (payload?: unknown) => void> = {}

vi.mock('../../api/client', () => ({
  getWardBrief: (...args: unknown[]) => getWardBrief(...args),
  socket: {
    on: (event: string, fn: (payload?: unknown) => void) => { handlers[event] = fn },
    off: (event: string) => { delete handlers[event] },
  },
}))
vi.mock('../../lib/orbBus', () => ({ openPatient: vi.fn() }))

import WardBriefCard from './WardBrief'

function brief(overrides: Record<string, unknown> = {}, rest: Record<string, unknown> = {}) {
  return {
    snapshot: {
      census: 42,
      risk_distribution: { critical: 1 },
      watchlist: [{ patient_id: 'P1', name: 'Asha Rao', bed: '12', ward: 'GM', level: 'critical', score: 80 }],
      recent_events: [],
      active_alerts: 2,
      as_of: '2026-10-01T08:00:00+00:00',
      ...overrides,
    },
    narrative: null,
    narrative_engine: null,
    generated_at: '2026-10-01T08:00:00+00:00',
    ...rest,
  }
}

async function flush() {
  await act(async () => { await Promise.resolve() })
}

describe('WardBrief', () => {
  beforeEach(() => {
    getWardBrief.mockReset()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('a scoped brief with census null names its scope, not a blank count', async () => {
    getWardBrief.mockResolvedValue(brief({ census: null, active_alerts: null, scope: 'General Medicine' }, {
      narrative: null, narrative_engine: 'local/medgemma:4b',
    }))
    render(<WardBriefCard />)
    expect(await screen.findByText('General Medicine only')).toBeInTheDocument()
    expect(screen.queryByText(/admitted hospital-wide/)).not.toBeInTheDocument()
    expect(screen.queryByText(/active$/)).not.toBeInTheDocument()
    // No narrative on screen, so no claim that one was narrated.
    expect(screen.getByText('Deterministic snapshot (local)')).toBeInTheDocument()
  })

  it('an unscoped brief still shows the hospital-wide census', async () => {
    getWardBrief.mockResolvedValue(brief())
    render(<WardBriefCard />)
    expect(await screen.findByText('42 admitted hospital-wide')).toBeInTheDocument()
  })

  it('coalesces a burst of socket events into one refetch and skips one already seen', async () => {
    vi.useFakeTimers()
    getWardBrief.mockResolvedValue(brief())
    render(<WardBriefCard />)
    await flush()
    expect(getWardBrief).toHaveBeenCalledTimes(1)

    // The snapshot already on screen: nothing to fetch.
    act(() => { handlers['sage:ward_brief']?.({ updated_at: '2026-10-01T08:00:00+00:00' }) })
    act(() => { vi.advanceTimersByTime(10_000) })
    expect(getWardBrief).toHaveBeenCalledTimes(1)

    // Five newer events in quick succession: one refetch.
    for (let i = 1; i <= 5; i++) {
      act(() => { handlers['sage:ward_brief']?.({ updated_at: `2026-10-01T08:0${i}:00+00:00` }) })
    }
    act(() => { vi.advanceTimersByTime(10_000) })
    await flush()
    expect(getWardBrief).toHaveBeenCalledTimes(2)
  })
})
