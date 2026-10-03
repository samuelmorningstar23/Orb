/**
 * THE READ THAT BURIED A FINISHED ASSESSMENT.
 *
 * One patient has ONE case room, and every critical Vigil alert writes another
 * assessment row into it. This module used to hand the surface `rows[0]` and
 * throw the rest away, which turned "the newest alert has no assessment" into
 * "this patient has no assessment": on the ward, a `ready` draft written at
 * 16:25:12 was replaced on screen by a 16:26:53 sibling that had failed with
 * `queue_timeout`, and the chart said "No assessment was written" 101 seconds
 * after Sage had written one. The finished draft was still in the database and
 * still unresolved — and unreachable, because the approve/decline controls
 * belong to whichever row the surface renders.
 *
 * So what is pinned here is not a shape for its own sake: the read must hand
 * back every row in the room, and the newest row must still be reported as the
 * current one. Which of the siblings still needs a clinician is the surface's
 * decision (`partitionAssessments` in components/sage/SageAlertAssessment.tsx);
 * this file's job is that the facts survive the trip.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { SageAssessment } from './sageAssessment'

const mocks = vi.hoisted(() => ({
  apiFetch: vi.fn(),
  getChannels: vi.fn(),
}))

vi.mock('./client', () => ({
  apiFetch: mocks.apiFetch,
  getChannels: mocks.getChannels,
}))

import { getLatestAssessmentForPatient, listSageAssessments } from './sageAssessment'

const CHANNEL = 'ch-bed7'

function row(over: Partial<SageAssessment> & { id: string }): SageAssessment {
  return {
    patient_id: 'P007',
    channel_id: CHANNEL,
    message_id: null,
    lane: 'ward',
    state: 'ready',
    version: 1,
    unavailable_reason: null,
    unavailable_message: null,
    fallback_text: null,
    news2: 19,
    news2_level: 'critical',
    news2_drivers: [],
    whats_happening: 'Rapid respiratory deterioration.',
    considerations: [],
    evidence: [],
    citation_basis: null,
    evidence_note: null,
    watch_items: [],
    confidence: 'medium',
    model: 'medgemma:27b',
    generated_ms: null,
    ttft_ms: null,
    created_at: '2026-08-14T16:25:12Z',
    updated_at: '2026-08-14T16:25:12Z',
    disclaimer: 'Draft decision support. Nothing here has been ordered, charted or administered.',
    ...over,
  }
}

/** 16:26:53 — the newer alert, whose draft never arrived. */
const FAILED_NEWEST = row({
  id: 'asm-1626',
  state: 'unavailable',
  unavailable_reason: 'queue_timeout',
  unavailable_message: 'Sage was still busy when this alert timed out. No assessment was written.',
  whats_happening: null,
  created_at: '2026-08-14T16:26:53Z',
})
/** 16:25:12 — written, complete, nobody has ruled on it. */
const READY_OLDER = row({ id: 'asm-1625', state: 'ready', version: 2 })

beforeEach(() => {
  vi.clearAllMocks()
  mocks.getChannels.mockResolvedValue([
    { id: 'ch-ward', type: 'ward', patient_id: null },
    { id: CHANNEL, type: 'case_room', patient_id: 'P007' },
  ])
})

describe('getLatestAssessmentForPatient', () => {
  it('hands back EVERY row in the room, not just the newest', async () => {
    mocks.apiFetch.mockResolvedValue([FAILED_NEWEST, READY_OLDER])

    const found = await getLatestAssessmentForPatient('P007')

    // The newest row is still the current one — the latest alert genuinely has
    // no assessment, and saying otherwise would be the opposite lie.
    expect(found.assessment?.id).toBe('asm-1626')
    expect(found.assessment?.state).toBe('unavailable')
    // THE DEFECT: the finished 16:25 draft must come back too. Returning only
    // the head is what made it unreachable on the ward.
    expect(found.rows.map(r => r.id)).toEqual(['asm-1626', 'asm-1625'])
    expect(found.rows.some(r => r.state === 'ready')).toBe(true)
    expect(found.channelId).toBe(CHANNEL)
  })

  it('does not reorder or filter what the server sent', async () => {
    // The endpoint owns the ordering, because that is where the timestamps are
    // canonical. A second ordering rule here is a second thing to disagree.
    const rows = [FAILED_NEWEST, READY_OLDER, row({ id: 'asm-old', state: 'approved' })]
    mocks.apiFetch.mockResolvedValue(rows)

    const found = await getLatestAssessmentForPatient('P007')

    expect(found.rows.map(r => r.id)).toEqual(['asm-1626', 'asm-1625', 'asm-old'])
  })

  it('reads the case room for this patient and nobody else', async () => {
    mocks.apiFetch.mockResolvedValue([READY_OLDER])
    await getLatestAssessmentForPatient('P007')
    expect(mocks.apiFetch).toHaveBeenCalledWith('/relay/channels/ch-bed7/sage_assessments')
  })

  it('reports "no case room" and "no draft" as two different facts', async () => {
    mocks.getChannels.mockResolvedValue([{ id: 'ch-ward', type: 'ward', patient_id: null }])
    await expect(getLatestAssessmentForPatient('P007')).resolves.toEqual({
      channelId: null, assessment: null, rows: [],
    })

    mocks.getChannels.mockResolvedValue([{ id: CHANNEL, type: 'case_room', patient_id: 'P007' }])
    mocks.apiFetch.mockResolvedValue([])
    await expect(getLatestAssessmentForPatient('P007')).resolves.toEqual({
      channelId: CHANNEL, assessment: null, rows: [],
    })
  })
})

describe('listSageAssessments', () => {
  it('narrows to one alert only when asked to', async () => {
    mocks.apiFetch.mockResolvedValue([])
    await listSageAssessments(CHANNEL)
    expect(mocks.apiFetch).toHaveBeenCalledWith('/relay/channels/ch-bed7/sage_assessments')

    await listSageAssessments(CHANNEL, 4021)
    expect(mocks.apiFetch).toHaveBeenCalledWith(
      '/relay/channels/ch-bed7/sage_assessments?message_id=4021',
    )
  })
})
