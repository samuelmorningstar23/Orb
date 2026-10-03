/**
 * "WHAT CHANGED SINCE I LAST LOOKED" — the review-watermark layer on the story.
 *
 * The marker is only useful if a clinician can trust it, and there are exactly
 * four ways to make it untrustworthy. All four are pinned here:
 *
 *  1. Treating "never reviewed" as "everything is new". That lights up every
 *     event on every chart at first login and teaches the clinician within a
 *     day to ignore the marker entirely.
 *  2. Advancing the watermark on RENDER. Then it answers "has this been drawn"
 *     rather than "has this been read", and a chart glanced at from a corridor
 *     silently clears its own badge.
 *  3. Marking reviewed as of NOW rather than as of the newest event actually
 *     SHOWN. Anything charted between the render and the click is then marked
 *     read by a clinician who never saw it — the one failure here with a real
 *     patient-safety edge.
 *  4. Failing the save silently. The badge disappears, the clinician believes
 *     it is cleared, and it is back on the next load.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const getPatientStoryFeed = vi.fn()
const markChartReviewed = vi.fn()

vi.mock('../../api/client', () => ({
  getPatientStoryFeed: (...a: unknown[]) => getPatientStoryFeed(...a),
  markChartReviewed: (...a: unknown[]) => markChartReviewed(...a),
  socket: { on: vi.fn(), off: vi.fn(), emit: vi.fn() },
}))

import PatientStory from './PatientStory'

const EVENTS = [
  { timestamp: '2026-08-08T09:00:00Z', type: 'order' as const, title: 'Ordered — Blood cultures', detail: 'LAB · STAT', actor: 'Dr Rao' },
  { timestamp: '2026-08-08T08:00:00Z', type: 'administration' as const, title: 'Gave Ceftriaxone', detail: '2g IV', actor: 'Nurse Iyer' },
  { timestamp: '2026-08-07T22:00:00Z', type: 'note' as const, title: 'PROGRESS note', detail: 'Settled overnight', actor: 'Dr Rao' },
  { timestamp: '2026-08-07T20:00:00Z', type: 'admission' as const, title: 'Admitted', detail: 'SW-1 · Bed SW-104', actor: null },
]

function feed(over: Record<string, unknown> = {}) {
  return { events: EVENTS, last_reviewed_at: null, new_count: 0, ...over }
}

beforeEach(() => {
  getPatientStoryFeed.mockReset()
  markChartReviewed.mockReset()
})

async function open(feedValue: ReturnType<typeof feed>) {
  getPatientStoryFeed.mockResolvedValue(feedValue)
  render(<PatientStory patientId="P001" patientName="Geeta Sharma" onClose={() => {}} />)
  await waitFor(() => expect(screen.getByRole('status')).toBeInTheDocument())
}

describe('the "since you last looked" marker', () => {
  it('shows NO new-since line when this clinician has never reviewed the chart', async () => {
    await open(feed({ last_reviewed_at: null }))

    expect(screen.getByRole('status')).toHaveTextContent(/have not marked this chart reviewed/i)
    // Not one NEW tag, and no boundary line — an absent watermark is not epoch zero.
    expect(screen.queryByText('NEW')).toBeNull()
    expect(screen.queryByText(/Reviewed to here/)).toBeNull()
  })

  it('marks exactly the events after the watermark as new', async () => {
    // Watermark sits between the note (22:00 on the 7th) and the eMAR (08:00).
    await open(feed({ last_reviewed_at: '2026-08-08T00:00:00Z', new_count: 2 }))

    expect(screen.getByRole('status')).toHaveTextContent(/2 new/)
    expect(screen.getAllByText('NEW')).toHaveLength(2)
    // The boundary is drawn once, on the first already-seen event.
    expect(screen.getAllByText(/Reviewed to here/)).toHaveLength(1)
  })

  it('says "nothing new" rather than showing an empty line when the chart is current', async () => {
    await open(feed({ last_reviewed_at: '2026-08-09T00:00:00Z', new_count: 0 }))

    expect(screen.getByRole('status')).toHaveTextContent(/Nothing new since you reviewed/i)
    expect(screen.queryByText('NEW')).toBeNull()
  })

  it('does not advance the watermark just because the story rendered', async () => {
    await open(feed({ last_reviewed_at: '2026-08-08T00:00:00Z', new_count: 2 }))
    expect(markChartReviewed).not.toHaveBeenCalled()
  })

  it('marks reviewed as of the newest event SHOWN, not as of now', async () => {
    /* The safety-relevant one. If the client sent `now`, an observation charted
       between this render and the click would be marked read by a clinician who
       never saw it. */
    await open(feed({ last_reviewed_at: '2026-08-08T00:00:00Z', new_count: 2 }))
    markChartReviewed.mockResolvedValue({
      last_reviewed_at: '2026-08-08T09:00:00Z', previous: '2026-08-08T00:00:00Z', new_count: 0,
    })

    fireEvent.click(screen.getByRole('button', { name: /Mark reviewed/i }))

    expect(markChartReviewed).toHaveBeenCalledWith('P001', '2026-08-08T09:00:00Z')
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent(/Nothing new since you reviewed/i),
    )
    expect(screen.queryByText('NEW')).toBeNull()
  })

  it('says so when the review marker could not be saved', async () => {
    await open(feed({ last_reviewed_at: '2026-08-08T00:00:00Z', new_count: 2 }))
    markChartReviewed.mockRejectedValue(new Error('503 Service Unavailable'))

    fireEvent.click(screen.getByRole('button', { name: /Mark reviewed/i }))

    const alert = await waitFor(() => screen.getByRole('alert'))
    expect(alert).toHaveTextContent(/Could not save the review marker|503/)
    // The badge must NOT clear on a failed save — it is back on the next load,
    // and a clinician who saw it vanish would stop believing it.
    expect(screen.getAllByText('NEW')).toHaveLength(2)
  })

  it('keeps the line honest under a type filter', async () => {
    /* Filter to eMAR only: the one new administration stays new, the new ORDER
       leaves the list, and the line follows the visible list rather than
       claiming two new rows above one. */
    await open(feed({ last_reviewed_at: '2026-08-08T00:00:00Z', new_count: 2 }))

    fireEvent.click(screen.getByRole('button', { name: /^eMAR$/ }))

    expect(screen.getAllByText('NEW')).toHaveLength(1)
    expect(screen.getByRole('status')).toHaveTextContent(/1 new/)
  })

  it('renders the streams that were added to the story without crashing', async () => {
    /* PatientStory indexes its label/colour map directly, so a type with no
       entry is a TypeError, not a missing chip. These five arrived in the same
       change that fixed the ordering bug. */
    const extra = [
      { timestamp: '2026-08-08T07:00:00Z', type: 'observation' as const, title: 'Serum lactate', detail: '4.2 mmol/L · HH', actor: null },
      { timestamp: '2026-08-08T06:00:00Z', type: 'bundle' as const, title: 'Sepsis6 bundle started', detail: 'NEWS2 >= 7', actor: null },
      { timestamp: '2026-08-08T05:00:00Z', type: 'problem' as const, title: 'Problem added — Septic shock', detail: 'Code R65.21', actor: null },
      { timestamp: '2026-08-08T04:00:00Z', type: 'allergy' as const, title: 'Allergy recorded — ceftriaxone', detail: 'anaphylaxis · severity severe', actor: null },
      { timestamp: '2026-08-08T03:00:00Z', type: 'order_event' as const, title: 'Order active → cancelled', detail: 'Duplicate', actor: null },
    ]
    await open(feed({ events: extra, last_reviewed_at: null }))

    expect(screen.getByText('Serum lactate')).toBeInTheDocument()
    expect(screen.getByText(/Sepsis6 bundle started/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Labs$/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^Allergies$/ })).toBeInTheDocument()
  })
})
