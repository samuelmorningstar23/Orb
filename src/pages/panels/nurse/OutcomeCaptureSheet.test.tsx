/**
 * The end-of-shift capture screen is the pilot's evidence instrument, so the
 * properties tested here are clinical-record properties, not widget behaviour:
 *
 *   1. one tap or ten, exactly ONE "no events" confirmation goes to the server
 *      (the backend dedupes too, but the double-submit must die at the source);
 *   2. a failed write is LOUD and never optimistic — a nurse who believes an
 *      unrecorded confirmation was recorded has silently turned her event-free
 *      shift into uncovered time;
 *   3. an already-confirmed shift says so — by whom, at when — and offers no
 *      second confirm;
 *   4. the outcome form posts the POST /outcome schema exactly.
 */
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const getShiftStatus = vi.fn()
const confirmNoEvents = vi.fn()
const recordOutcome = vi.fn()

vi.mock('../../../api/shadowOutcome', () => ({
  getShiftStatus: (...args: unknown[]) => getShiftStatus(...args),
  confirmNoEvents: (...args: unknown[]) => confirmNoEvents(...args),
  recordOutcome: (...args: unknown[]) => recordOutcome(...args),
}))

import OutcomeCaptureSheet from './OutcomeCaptureSheet'

const WARD = 'General Medicine'

function shiftStatus(wardOverrides: Record<string, unknown> = {}) {
  return {
    generated_at: '2026-08-10T14:00:00.000+00:00',
    status_available: true,
    shifts: [{
      shift: 'morning',
      period_start: '2026-08-10T07:00:00.000+00:00',
      period_end: '2026-08-10T15:00:00.000+00:00',
      label: '07:00–15:00',
      is_current: true,
      wards: [{
        ward: WARD,
        confirmed: false,
        confirmed_at: null,
        confirmed_by_role: null,
        events_recorded: 0,
        status: 'outstanding',
        ...wardOverrides,
      }],
    }],
    max_backdate_hours: 72,
    event_types: ['icu_transfer', 'rapid_response_call', 'code_blue', 'death',
                  'unplanned_escalation_of_care'],
    sources: ['witnessed', 'chart_review', 'handover'],
    recognition: ['clinician_independent', 'prompted_by_orb', 'unknown'],
  }
}

/** Sign a nurse in the way api/client stores it — the ward field prefills from
 *  the stored department, which is what triggers the shift-status load. */
function signIn() {
  localStorage.setItem('orb_user', JSON.stringify({
    id: 'NUR001', name: 'Nurse', role: 'nurse', department: WARD,
  }))
}

beforeEach(() => {
  localStorage.clear()
  getShiftStatus.mockReset()
  confirmNoEvents.mockReset()
  recordOutcome.mockReset()
  getShiftStatus.mockResolvedValue(shiftStatus())
  signIn()
})

async function openConfirmStep() {
  render(<OutcomeCaptureSheet />)
  const start = await screen.findByRole(
    'button', { name: /no deterioration events this shift/i }, { timeout: 3000 },
  )
  await act(async () => { start.click() })
  return screen.getByRole('button', { name: /confirm: no events/i })
}

describe('the once-per-shift confirmation', () => {
  it('sends exactly one request when the confirm button is double-tapped', async () => {
    let resolve!: (v: unknown) => void
    confirmNoEvents.mockImplementation(() => new Promise(r => { resolve = r }))
    const confirm = await openConfirmStep()

    // Two taps inside one act(): both handlers run against the render where
    // `confirming` is still false — only the ref latch holds.
    await act(async () => {
      confirm.click()
      confirm.click()
    })
    expect(confirmNoEvents).toHaveBeenCalledTimes(1)
    // The POST body is the exact strings shift-status handed out — that string
    // equality is what makes the server's "already confirmed?" check exact.
    expect(confirmNoEvents).toHaveBeenCalledWith({
      ward: WARD,
      period_start: '2026-08-10T07:00:00.000+00:00',
      period_end: '2026-08-10T15:00:00.000+00:00',
      shift: 'morning',
    })

    await act(async () => {
      resolve({ recorded: true, episode_id: 'cov_x', ward: WARD, shift: 'morning' })
    })
  })

  it('surfaces a refused confirmation loudly and never claims success', async () => {
    confirmNoEvents.mockRejectedValue(
      new Error('The shift confirmation was NOT recorded (shadow instrument unavailable or disabled). This ward-shift will count as uncovered time.'),
    )
    const confirm = await openConfirmStep()
    await act(async () => { confirm.click() })

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/NOT recorded/i)
    // Never optimistic: no success wording anywhere on a failed write.
    expect(screen.queryByText(/^Recorded:/)).toBeNull()
    // The nurse can try again — the confirm step is still on screen.
    expect(screen.getByRole('button', { name: /confirm: no events/i })).toBeEnabled()
  })

  it('shows who confirmed and when, and offers no second confirm', async () => {
    // TODAY, not a pinned date. `clockTime` renders a same-day moment as a bare
    // clock time and anything older with its date in front ("Aug 10, 20:25") —
    // deliberately, so a days-old reading cannot be misread as an hour ago. The
    // pinned 2026-08-10 stamp therefore passed on the day it was written and
    // failed every day after it, on the assertion below rather than on anything
    // this test is about. A confirmation from the shift being displayed is also
    // the truer fixture: shift status only ever reports the current shift.
    // 08:25 on today's LOCAL date — fixed wall clock, so it can never drift
    // across local midnight the way "now minus 30 minutes" would.
    const confirmedAt = (() => {
      const d = new Date()
      d.setHours(8, 25, 0, 0)
      return d.toISOString()
    })()
    getShiftStatus.mockResolvedValue(shiftStatus({
      confirmed: true,
      confirmed_at: confirmedAt,
      confirmed_by_role: 'nurse',
      status: 'confirmed',
    }))
    render(<OutcomeCaptureSheet />)

    const note = await screen.findByText(/already confirmed/i, {}, { timeout: 3000 })
    expect(note.textContent).toMatch(/by the nurse/i)
    expect(note.textContent).toMatch(/at \d/)
    expect(screen.queryByRole('button', { name: /no deterioration events this shift/i })).toBeNull()
    expect(confirmNoEvents).not.toHaveBeenCalled()
  })
})

describe('the deterioration-event form', () => {
  it('posts the /outcome schema exactly as entered', async () => {
    recordOutcome.mockResolvedValue({
      recorded: true, shadow_event_id: 7, event_type: 'icu_transfer',
      source: 'witnessed', recognised_by: null, occurred_at: null, note: '',
    })
    render(<OutcomeCaptureSheet />)

    const open = await screen.findByRole(
      'button', { name: /record a deterioration event/i }, { timeout: 3000 },
    )
    await act(async () => { open.click() })

    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: 'P0042' } })
    fireEvent.change(screen.getByLabelText(/^event$/i), { target: { value: 'icu_transfer' } })
    const submit = screen.getByRole('button', { name: /record event/i })
    await act(async () => { submit.click() })

    expect(recordOutcome).toHaveBeenCalledTimes(1)
    expect(recordOutcome).toHaveBeenCalledWith({
      patient_id: 'P0042',
      event_type: 'icu_transfer',
      source: 'witnessed',
    })
    await waitFor(() => expect(screen.getByText(/ICU transfer for P0042/)).toBeInTheDocument())
  })

  it('refuses to submit without a patient and an event type', async () => {
    render(<OutcomeCaptureSheet />)
    const open = await screen.findByRole(
      'button', { name: /record a deterioration event/i }, { timeout: 3000 },
    )
    await act(async () => { open.click() })

    const submit = screen.getByRole('button', { name: /record event/i })
    expect(submit).toBeDisabled()
    await act(async () => { submit.click() })
    expect(recordOutcome).not.toHaveBeenCalled()
  })
})
