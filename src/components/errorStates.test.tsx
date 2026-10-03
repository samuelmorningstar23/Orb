/**
 * "NO DATA" AND "FAILED TO LOAD" MUST NOT LOOK THE SAME.
 *
 * A clinician who cannot tell an empty result from a broken request will read
 * the empty one as a clinical fact. Two surfaces got this wrong in opposite
 * directions and both are asserted here:
 *
 *   1. ClinicalCourseTimeline — VigilPanel swallowed the projection rejection,
 *      leaving `projection === null`, which IS the component's loading state.
 *      A failed projection therefore pulsed "Sage is charting the expected
 *      course…" forever, with no retry and no end.
 *
 *   2. SageAmbientSidebar — a failed fetch was logged to the console and
 *      nothing else, so the visibility check returned null and the entire
 *      risk-flag panel vanished. An absent deterioration warning reads as an
 *      all-clear. (WardBrief in the same product already says "this is a
 *      display failure, not an all-clear"; this surface did not.)
 */
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', () => ({
  getSageMemory: vi.fn(() => Promise.reject(new Error('503 Service Unavailable'))),
  getSageActions: vi.fn(() => Promise.reject(new Error('503 Service Unavailable'))),
  transitionSageAction: vi.fn(),
  socket: { on: vi.fn(), off: vi.fn(), emit: vi.fn() },
}))

import { ClinicalCourseTimeline } from './ClinicalCourseTimeline'
import SageAmbientSidebar from './relay/SageAmbientSidebar'

describe('failed loads are distinguishable from empty results', () => {
  it('clinical course says it failed instead of pulsing forever', () => {
    const { rerender } = render(<ClinicalCourseTimeline projection={null} />)
    // Genuinely still loading — the pulse is correct here.
    expect(screen.getByText(/Sage is charting the expected course/)).toBeInTheDocument()

    // The request failed. The clinician must be told, and offered a retry.
    const onRetry = vi.fn()
    rerender(<ClinicalCourseTimeline projection={null} failed onRetry={onRetry} />)

    expect(screen.queryByText(/Sage is charting the expected course/)).toBeNull()
    expect(screen.getByRole('alert')).toHaveTextContent(/Clinical course unavailable/)
    expect(screen.getByRole('button', { name: /Retry/ })).toBeInTheDocument()
  })

  it('sage ambient sidebar does not silently vanish on a failed fetch', async () => {
    render(<SageAmbientSidebar channelId="ch-bed7" />)

    const alert = await waitFor(() => screen.getByRole('alert'))
    expect(alert).toHaveTextContent(/could not load/i)
    // The crucial wording: absence of flags here is not a clinical statement.
    expect(alert).toHaveTextContent(/does not mean there are no risk flags/i)
    expect(screen.getByRole('button', { name: /Retry/ })).toBeInTheDocument()
  })
})
