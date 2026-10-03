/**
 * WHAT "MY SHIFT" IS ALLOWED TO SAY, AND HOW LOUDLY.
 *
 * These are not styling tests. Every assertion below is a clinical claim the
 * screen makes, and each one has a specific way of being wrong:
 *
 *   · CRY WOLF. The section header used to render "Overdue" in
 *     --status-danger-text on every shift, quiet or busy. A colour that is
 *     present on a good day cannot mean anything on a bad one — it is the
 *     mechanism that trains staff to stop looking at alerts. The label is
 *     constant; the COUNT CHIP carries the tint, and only above zero.
 *   · A GREEN ZERO. "0 obs overdue" tinted green says "actively achieved". It
 *     is not an achievement, it is the absence of a problem, and spending the
 *     ok colour on it devalues the one place it is earned.
 *   · AN EMPTY BOX FOR THREE DIFFERENT FACTS. "nothing is due", "we could not
 *     find out" and "still loading" used to be the same grey dashed box. Those
 *     are opposite clinical claims. A nurse who cannot tell them apart will
 *     trust none of them.
 *   · A QUIET SHIFT THAT LOOKS LIKE A FAILURE. "Nothing due" is GOOD NEWS at
 *     03:00 and has to read as reassurance, with the next due time named — so
 *     that "nothing" becomes "nothing YET, and here is when".
 *   · A STALE BOARD THAT LOOKS LIVE. A tablet asleep in a pocket stops polling.
 *     The board on screen keeps saying "updated 09:14" and she reads it as now.
 */
import { render, screen, act } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { NursePatientCard, ShiftBoard } from '../../../api/nurse'
import { clearOfflineCache } from '../../../lib/offline/phiCache'

const getShiftBoard = vi.fn()

vi.mock('../../../api/nurse', () => ({
  getShiftBoard: (...a: unknown[]) => getShiftBoard(...a),
  recordObservations: vi.fn(),
  previewNews2: vi.fn(),
  currentUserName: () => 'Nurse A',
}))

import NursePanel from './NursePanel'

/* Read from disk, not imported: vitest runs with `css: false`, so the cascade
   this file asserts on does not exist in jsdom at all. The invariants below
   live in the stylesheet, so that is where they are checked. */
const NURSE_CSS = readFileSync(
  join(process.cwd(), 'src/pages/panels/nurse/NursePanel.css'),
  'utf8',
)

const NURSE_A = 'NUR001'

function signIn() {
  localStorage.setItem('orb_user', JSON.stringify({ id: NURSE_A, name: 'Nurse A', role: 'nurse' }))
}

function patient(over: Partial<NursePatientCard> = {}): NursePatientCard {
  return {
    patient_id: 'P001',
    name: 'Asha Menon',
    mrn: 'MRN-1',
    bed: 'GM-07',
    ward: 'GM',
    age: 61,
    sex: 'F',
    diagnosis: 'Community-acquired pneumonia',
    department: 'General Medicine',
    attending_doctor_name: 'Dr Rao',
    on_scale_2: false,
    allergies: [],
    news2: {
      score: 0.2, level: 'stable', raw_news2: 1, single_param_3: false,
      drivers: [], breakdown: [], scoring_model: 'NEWS2', spo2_scale: 1,
    },
    last_obs_at: null,
    obs_interval_minutes: 240,
    obs_band: 'Low',
    obs_response: 'Routine',
    obs_due_at: null,
    obs_minutes_until_due: null,
    obs_overdue: false,
    obs_due_soon: false,
    medications: [],
    meds_overdue: 0,
    meds_due_soon: 0,
    ...over,
  }
}

function board(over: Partial<ShiftBoard> = {}): ShiftBoard {
  return {
    generated_at: new Date().toISOString(),
    department: 'General Medicine',
    patients: [],
    totals: { patients: 0, critical: 0, elevated: 0, obs_overdue: 0, meds_overdue: 0 },
    ...over,
  }
}

/** A quiet ward: one stable patient, obs not due for another 76 minutes. */
function quietBoard(): ShiftBoard {
  const due = new Date(Date.now() + 76 * 60_000).toISOString()
  return board({
    patients: [patient({ obs_due_at: due, obs_minutes_until_due: 76 })],
    totals: { patients: 1, critical: 0, elevated: 0, obs_overdue: 0, meds_overdue: 0 },
  })
}

/** A bad ward: obs 42 minutes late on a critical patient, plus a late dose. */
function busyBoard(): ShiftBoard {
  return board({
    patients: [
      patient({
        patient_id: 'P001', name: 'Asha Menon', bed: 'GM-04',
        news2: {
          score: 0.9, level: 'critical', raw_news2: 9, single_param_3: false,
          drivers: ['RR'], breakdown: [], scoring_model: 'NEWS2', spo2_scale: 1,
        },
        obs_interval_minutes: 30, obs_band: 'critical',
        obs_minutes_until_due: -42, obs_overdue: true,
      }),
      patient({
        patient_id: 'P002', name: 'Vikas Shah', bed: 'GM-11',
        news2: {
          score: 0.5, level: 'elevated', raw_news2: 5, single_param_3: false,
          drivers: [], breakdown: [], scoring_model: 'NEWS2', spo2_scale: 1,
        },
        obs_minutes_until_due: 180,
        medications: [{
          medication_id: 77, drug_name: 'Piperacillin-tazobactam',
          dosage: '4.5 g', route: 'IV', frequency: '8-hourly',
          schedule_kind: 'scheduled', interval_minutes: 480, status: 'active',
          last_given_at: null, given_count: 0, due_at: null, due_basis: 'order_start',
          missed_slots: 1, minutes_until_due: -12, overdue: true, due_soon: false,
          unverified: false,
        }],
        meds_overdue: 1,
      }),
    ],
    totals: { patients: 2, critical: 1, elevated: 1, obs_overdue: 1, meds_overdue: 1 },
  })
}

async function mount() {
  const view = render(<NursePanel />)
  await act(async () => {})
  return view
}

beforeEach(() => {
  localStorage.clear()
  getShiftBoard.mockReset()
  Object.defineProperty(navigator, 'onLine', { value: true, configurable: true })
  signIn()
})

afterEach(() => {
  vi.useRealTimers()
  clearOfflineCache()
  localStorage.clear()
})

/* ════════════════════════════════════════════════════════════════════════
   THE CRY-WOLF RULE
   ════════════════════════════════════════════════════════════════════════ */

describe('the section label never carries a status colour', () => {
  it('renders "Overdue" as a plain label and puts the tint on its count chip', async () => {
    getShiftBoard.mockResolvedValue(busyBoard())
    const { container } = await mount()

    const heading = screen.getByRole('heading', { name: /^Overdue/ })

    // The label itself is the constant micro-caps class and NOTHING else. Any
    // status modifier here is the defect: "OVERDUE" in red on a shift with two
    // late obs is the same red as a shift with none.
    expect(heading.className.split(/\s+/).filter(Boolean)).toEqual(['orb-section-header__label'])

    // The count is where the escalation is allowed to live.
    const chip = heading.querySelector('.orb-count-chip')
    expect(chip).not.toBeNull()
    expect(chip!.className).toContain('orb-count-chip--danger')
    expect(chip!.textContent).toBe('2 tasks')

    // And nothing inside the heading other than the chip carries a status class.
    const tinted = [...container.querySelectorAll('h2 *')]
      .filter(el => /--danger|--warn|--critical|--soon/.test(el.className))
    expect(tinted.map(el => el.className)).toEqual([expect.stringContaining('orb-count-chip--danger')])
  })

  it('gives the "due within 30 minutes" label the same neutral treatment', async () => {
    getShiftBoard.mockResolvedValue(board({
      patients: [patient({ obs_minutes_until_due: 18, obs_due_soon: true })],
      totals: { patients: 1, critical: 0, elevated: 0, obs_overdue: 0, meds_overdue: 0 },
    }))
    await mount()

    const heading = screen.getByRole('heading', { name: /^Due within 30 minutes/ })
    expect(heading.className.split(/\s+/).filter(Boolean)).toEqual(['orb-section-header__label'])
    expect(heading.querySelector('.orb-count-chip')!.className).toContain('orb-count-chip--warn')
  })

  /* The DOM test above only proves the class is absent. This proves no rule
     exists that could colour a label even if one came back — the stylesheet
     half of the same invariant, which a render test with `css: false` can
     never see. */
  it('has no rule in NursePanel.css that paints a section label a status colour', () => {
    const css = NURSE_CSS.replace(/\/\*[\s\S]*?\*\//g, '')

    expect(css).not.toMatch(/\.nurse-section__title--danger/)

    const offenders = css
      .split('}')
      .filter(block => {
        const selector = block.split('{')[0]
        return /(section__title|section-header|__label)/.test(selector)
          && /color:\s*var\(--status-/.test(block)
      })
      .map(block => block.split('{')[0].trim())
    expect(offenders).toEqual([])
  })
})

/* ════════════════════════════════════════════════════════════════════════
   ZERO IS NEVER GREEN
   ════════════════════════════════════════════════════════════════════════ */

describe('adverse counts', () => {
  it('renders every zero neutral — never green, never tinted', async () => {
    getShiftBoard.mockResolvedValue(quietBoard())
    const { container } = await mount()

    const chips = [...container.querySelectorAll('.nurse-totals .orb-count-chip')]
    expect(chips).toHaveLength(4)
    for (const chip of chips) {
      expect(chip.className.split(/\s+/).filter(Boolean), chip.textContent ?? '')
        .toEqual(['orb-count-chip'])
    }
    // Green is "actively achieved", not "not currently bad".
    expect(container.innerHTML).not.toMatch(/status-ok|--ok\b|count-chip--success/)
  })

  it('tints only the counters that are actually above zero', async () => {
    getShiftBoard.mockResolvedValue(board({
      patients: [],
      totals: { patients: 5, critical: 0, elevated: 2, obs_overdue: 0, meds_overdue: 3 },
    }))
    const { container } = await mount()

    const chips = [...container.querySelectorAll('.nurse-totals .orb-count-chip')]
    const tinted = chips.map(c => /--danger|--warn/.test(c.className))
    //           critical(0)  elevated(2)  obs overdue(0)  doses overdue(3)
    expect(tinted).toEqual([false, true, false, true])
  })

  it('says what the number counts, so a screen reader hears a sentence', async () => {
    getShiftBoard.mockResolvedValue(busyBoard())
    const { container } = await mount()

    const strip = container.querySelector('.nurse-totals')!
    // "obs" is ward shorthand a screen reader should not be left to guess at.
    expect(strip.textContent).toContain('observations overdue')
    // One late dose is "1 dose overdue", never "1 doses overdue".
    expect(strip.textContent).toContain('1dose overdue')
    expect(strip.textContent).not.toContain('doses overdue')
  })

  it('says "doses" when there is more than one', async () => {
    getShiftBoard.mockResolvedValue(board({
      patients: [],
      totals: { patients: 5, critical: 0, elevated: 0, obs_overdue: 0, meds_overdue: 3 },
    }))
    const { container } = await mount()
    expect(container.querySelector('.nurse-totals')!.textContent).toContain('3doses overdue')
  })
})

/* ════════════════════════════════════════════════════════════════════════
   THE CALM EMPTY STATE — the most important state on this screen
   ════════════════════════════════════════════════════════════════════════ */

describe('"nothing due" reads as reassurance, not as an error or a blank', () => {
  it('uses the solid calm block and names the next thing that falls due', async () => {
    // Freeze the clock mid-afternoon. quietBoard() schedules the next obs 76
    // minutes from NOW, and clockTime() date-prefixes anything not same-day —
    // so unfrozen, this test failed every night between ~22:44 and midnight
    // (found live 2026-08-18 at 23:32: "Aug 19, 12:50 AM"). The cross-midnight
    // face is pinned by its own test below.
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-18T14:00:00'))
    getShiftBoard.mockResolvedValue(quietBoard())
    const { container } = await mount()

    const state = container.querySelector('.orb-state')!
    // Solid, not the dashed box that means "we found nothing". Dashed says
    // absence; solid says this is the answer.
    expect(state.className).toContain('orb-state--calm')
    expect(state.className).not.toContain('orb-state--error')
    expect(state.getAttribute('role')).toBeNull()

    expect(screen.getByText('Nothing due right now')).toBeInTheDocument()

    // Line 2 is load-bearing: it converts "nothing" into "nothing YET".
    const detail = container.querySelector('.orb-state__detail')!
    expect(detail.textContent).toMatch(/Next observations for GM-07 at \d{1,2}[:.]\d{2}/)
    // …and the clock time is mono, because she compares it against her watch.
    expect(detail.querySelector('.orb-home__clock')).not.toBeNull()

    // Line 3 names the scope, the count and the freshness. The time it was
    // updated is the evidence; "Nothing failed to load" was a double negative
    // standing in for it.
    const reassure = container.querySelector('.orb-state__reassure')!
    expect(reassure.textContent).toContain('Live view of General Medicine')
    expect(reassure.textContent).toContain('1 patients')
    expect(reassure.textContent).toMatch(/updated \d{1,2}[:.]\d{2}/)
    expect(reassure.querySelector('.orb-home__clock')).not.toBeNull()
    expect(reassure.textContent).not.toContain('Nothing failed to load')
  })

  it('names the day, not just the clock, when the next due time falls tomorrow', async () => {
    // The face the frozen test above cannot see: at 23:40 the next obs lands
    // after midnight, and a bare "12:56 AM" beside a wall clock reading 11:40 PM
    // is ambiguous in the worst way. clockTime() prefixes the date exactly then
    // (same-day check in nurse/time.ts) — behavior found correct in the wild on
    // 2026-08-18 and pinned here so nobody "fixes" it back to a bare time.
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-18T23:40:00'))
    getShiftBoard.mockResolvedValue(quietBoard())
    const { container } = await mount()

    const detail = container.querySelector('.orb-state__detail')!
    expect(detail.textContent).toMatch(
      /Next observations for GM-07 at \d{1,2} [A-Za-z]{3}, \d{1,2}[:.]\d{2}/,
    )
  })

  it('never congratulates, and never uses an exclamation mark or an emoji', async () => {
    getShiftBoard.mockResolvedValue(quietBoard())
    const { container } = await mount()

    const text = container.querySelector('.orb-state')!.textContent ?? ''
    expect(text).not.toMatch(/!/)
    expect(text).not.toMatch(/caught up|well done|great|nice work/i)
    // No pictographs: a nurse at 3am does not want to be congratulated by
    // software, she wants to know the screen is telling the truth.
    expect(text).not.toMatch(/\p{Extended_Pictographic}/u)
  })

  it('still states a horizon when nothing at all is scheduled', async () => {
    getShiftBoard.mockResolvedValue(board({
      patients: [patient({ obs_minutes_until_due: null, obs_due_at: null })],
      totals: { patients: 1, critical: 0, elevated: 0, obs_overdue: 0, meds_overdue: 0 },
    }))
    const { container } = await mount()

    // Never omitted. "Nothing due" with no horizon is indistinguishable from a
    // queue that silently failed to build.
    expect(container.querySelector('.orb-state__detail')!.textContent)
      .toBe('No observations or doses are scheduled in the next four hours.')
  })

  it('does not claim the view is live when it is a cached copy', async () => {
    getShiftBoard.mockResolvedValue(quietBoard())
    const { container } = await mount()
    expect(container.querySelector('.orb-state__reassure')!.textContent)
      .toContain('Live view')

    // Same quiet ward, now read from a copy: the sentence must change with it.
    act(() => {
      Object.defineProperty(navigator, 'onLine', { value: false, configurable: true })
      window.dispatchEvent(new Event('offline'))
    })
    expect(container.querySelector('.orb-state__reassure')!.textContent)
      .toContain('Saved copy')
  })

  it('is not the same shape as the "no patients in scope" state', async () => {
    getShiftBoard.mockResolvedValue(board({
      totals: { patients: 0, critical: 0, elevated: 0, obs_overdue: 0, meds_overdue: 0 },
    }))
    const { container } = await mount()

    await act(async () => { screen.getByRole('tab', { name: /My patients/ }).click() })

    const state = container.querySelector('#nurse-panel-patients .orb-state')!
    // Dashed absence, NOT calm: an empty ward is far more often a scope problem
    // than a genuinely empty ward, and the copy has to say so.
    expect(state.className).toContain('orb-state--empty')
    expect(state.className).not.toContain('orb-state--calm')
    expect(state.textContent).toContain('this may be a scope problem, not an empty ward')
    // When it was checked, never the double negative "Nothing failed to load".
    const reassure = state.querySelector('.orb-state__reassure')!
    expect(reassure.textContent).toMatch(/^Checked at .+\.$/)
    expect(reassure.querySelector('.orb-home__clock')).not.toBeNull()
    expect(state.textContent).not.toContain('Nothing failed to load')
  })
})

/* ════════════════════════════════════════════════════════════════════════
   LOADING
   ════════════════════════════════════════════════════════════════════════ */

describe('loading', () => {
  it('shows skeletons in the row geometry, not a sentence', async () => {
    let release: (b: ShiftBoard) => void = () => {}
    getShiftBoard.mockImplementation(() => new Promise<ShiftBoard>(res => { release = res }))

    const { container } = render(<NursePanel />)

    const loading = container.querySelector('[role="status"][aria-busy="true"]')
    expect(loading).not.toBeNull()

    // Skeletons only, never a spinner, never a full-page "Loading…".
    expect(container.querySelectorAll('.orb-skeleton__bar').length).toBeGreaterThan(0)
    // The skeleton wears the SAME chassis as a loaded row, so nothing shifts
    // when the data lands.
    expect(container.querySelectorAll('.orb-row').length).toBe(3)
    expect(container.querySelector('.nurse-empty')).toBeNull()

    await act(async () => { release(busyBoard()) })
    expect(container.querySelectorAll('.orb-skeleton__bar').length).toBe(0)
  })

  it('never replaces content with skeletons on a refresh', async () => {
    // The 60s poll and the Refresh button must update in place. Blanking a
    // board a nurse is reading, once a minute, is how a screen becomes unusable.
    getShiftBoard.mockResolvedValue(busyBoard())
    const { container } = await mount()
    expect(screen.getByText(/GM-04/)).toBeInTheDocument()

    let release: (b: ShiftBoard) => void = () => {}
    getShiftBoard.mockImplementation(() => new Promise<ShiftBoard>(res => { release = res }))
    act(() => { screen.getByRole('button', { name: 'Refresh' }).click() })

    expect(container.querySelectorAll('.orb-skeleton__bar').length).toBe(0)
    expect(screen.getByText(/GM-04/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Refreshing/ })).toBeDisabled()

    await act(async () => { release(busyBoard()) })
  })
})

/* ════════════════════════════════════════════════════════════════════════
   ERROR — the state that must never look like an all-clear
   ════════════════════════════════════════════════════════════════════════ */

describe('a failed read', () => {
  it('says so loudly, keeps the real message, and refuses to imply an all-clear', async () => {
    getShiftBoard.mockRejectedValue(new Error('Orb refused this read for your account (403)'))
    const { container } = await mount()

    const state = container.querySelector('.orb-state--error')!
    expect(state.getAttribute('role')).toBe('alert')
    expect(state.textContent).toContain('Orb refused this read for your account (403)')
    expect(state.textContent).toContain('This is a display failure, not an all-clear.')
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()

    // The failure must NOT be dressed as a quiet shift.
    expect(container.querySelector('.orb-state--calm')).toBeNull()
    expect(screen.queryByText('Nothing due right now')).toBeNull()
  })
})

/* ════════════════════════════════════════════════════════════════════════
   THE STALENESS LADDER
   ════════════════════════════════════════════════════════════════════════ */

describe('a board that has stopped refreshing while online', () => {
  /** Freeze the poll, jump the wall clock, then re-render the way a nurse
   *  would: by touching the screen. This is the tablet-asleep-in-a-pocket
   *  case — the poll never fired, so nothing corrected itself. */
  async function ageBy(minutes: number) {
    vi.useFakeTimers()
    getShiftBoard.mockResolvedValue(quietBoard())
    const view = render(<NursePanel />)
    await act(async () => {})
    vi.setSystemTime(Date.now() + minutes * 60_000)
    act(() => { view.container.querySelector<HTMLElement>('#nurse-tab-patients')!.click() })
    return view
  }

  it('stays quiet under three minutes — a banner for a two-minute-old board is crying wolf', async () => {
    const { container } = await ageBy(2)
    const context = container.querySelector('.orb-home__context')!
    expect(context.className).toBe('orb-home__context')
    expect(context.getAttribute('role')).toBeNull()
    expect(context.textContent).not.toMatch(/min ago/)
  })

  it('turns the whole context line warn between three and ten minutes', async () => {
    const { container } = await ageBy(6)
    const context = container.querySelector('.orb-home__context')!
    expect(context.className).toContain('orb-home__context--stale')
    expect(context.getAttribute('role')).toBe('status')
    expect(context.textContent).toContain('6 min ago')
    // The clock time never disappears — it is what she checks her watch against.
    expect(context.querySelector('.orb-home__clock')).not.toBeNull()
    expect(container.querySelector('.nurse-inline-retry')).toBeNull()
  })

  it('escalates past ten minutes and offers a Retry in the line itself', async () => {
    const { container } = await ageBy(14)
    const context = container.querySelector('.orb-home__context')!
    expect(context.className).toContain('orb-home__context--very-stale')
    expect(context.getAttribute('role')).toBe('alert')
    expect(context.textContent).toContain('14 min ago')
    expect(context.querySelector('.nurse-inline-retry')).not.toBeNull()
    expect(context.querySelector('.orb-home__clock')).not.toBeNull()
  })

  it('leaves the ladder alone when the view is already banner-labelled as a copy', async () => {
    // Offline is a different problem with a different treatment. Two labels for
    // one fact is how both stop being read.
    vi.useFakeTimers()
    getShiftBoard.mockResolvedValue(quietBoard())
    const { container } = render(<NursePanel />)
    await act(async () => {})
    act(() => {
      Object.defineProperty(navigator, 'onLine', { value: false, configurable: true })
      window.dispatchEvent(new Event('offline'))
    })
    vi.setSystemTime(Date.now() + 25 * 60_000)
    act(() => { container.querySelector<HTMLElement>('#nurse-tab-patients')!.click() })

    expect(container.querySelector('.orb-home__context')!.className).toBe('orb-home__context')
    expect(container.querySelector('[data-testid="offline-banner"]')).not.toBeNull()
  })
})

/* ════════════════════════════════════════════════════════════════════════
   THE ONE LIVE REGION
   ════════════════════════════════════════════════════════════════════════ */

describe('what gets announced', () => {
  it('speaks only when the overdue count goes UP, and never re-reads the summary', async () => {
    getShiftBoard.mockResolvedValue(quietBoard())
    const { container } = await mount()

    const region = [...container.querySelectorAll('[role="status"][aria-live="polite"]')]
      .find(el => el.classList.contains('sr-only'))!
    expect(region.textContent).toBe('')

    getShiftBoard.mockResolvedValue(busyBoard())
    await act(async () => { screen.getByRole('button', { name: 'Refresh' }).click() })
    expect(region.textContent).toBe('2 tasks are now overdue.')

    // Improvement is silent. A summary is not a change announcement, and a
    // 60-second poll that re-reads the ward once a minute is noise.
    getShiftBoard.mockResolvedValue(quietBoard())
    await act(async () => { screen.getByRole('button', { name: 'Refresh' }).click() })
    expect(region.textContent).toBe('2 tasks are now overdue.')
  })

  it('does not put the ward summary in a live region', async () => {
    getShiftBoard.mockResolvedValue(busyBoard())
    const { container } = await mount()
    const strip = container.querySelector('.nurse-totals')!
    expect(strip.getAttribute('role')).toBeNull()
    expect(strip.getAttribute('aria-live')).toBeNull()
  })
})

/* ════════════════════════════════════════════════════════════════════════
   THE ROWS THEMSELVES
   ════════════════════════════════════════════════════════════════════════ */

describe('the task queue', () => {
  it('builds every row on the shared chassis, with the status rule on the row', async () => {
    getShiftBoard.mockResolvedValue(busyBoard())
    const { container } = await mount()

    const rows = [...container.querySelectorAll('#nurse-panel-tasks .orb-row')]
    expect(rows.length).toBeGreaterThan(0)
    // The overdue obs on the critical patient sorts first: deterioration before
    // lateness. That ordering is clinical behaviour and is not mine to change.
    expect(rows[0].className).toContain('orb-row--overdue')
    expect(rows[0].querySelector('.orb-row__primary')!.textContent).toBe('GM-04 · Asha Menon')
    expect(rows[0].querySelector('.orb-row__value')!.textContent).toBe('42m late')
  })

  it('names the score, the task and the destination in the row label', async () => {
    getShiftBoard.mockResolvedValue(busyBoard())
    await mount()

    // The visible `›` affordance and the badge colour are both invisible to a
    // screen reader, so the row has to say all of it in words — and it must be
    // the SAME sentence the badge uses, never a second spelling of one score.
    const row = screen.getByRole('button', { name: /Piperacillin-tazobactam/ })
    const label = row.getAttribute('aria-label')!
    expect(label).toContain('Vikas Shah')
    expect(label).toContain('bed GM-11')
    expect(label).toContain('NEWS2 5 (elevated)')
    expect(label).toContain('12m late')
    expect(label).toContain('1 not charted')
    expect(label).toContain('open the medication round')
  })

  it('counts every row it holds in the "Due now" tab, not only the overdue ones', async () => {
    // Two overdue (obs on GM-04, the dose on GM-11) and one due soon. The tab
    // said "Due now (2)" over three rows; the overdue chip inside says "2".
    const b = busyBoard()
    b.patients.push(patient({
      patient_id: 'P003', name: 'Ravi Iyer', bed: 'GM-12',
      obs_minutes_until_due: 18, obs_due_soon: true,
    }))
    getShiftBoard.mockResolvedValue(b)
    const { container } = await mount()

    expect(container.querySelectorAll('#nurse-panel-tasks .orb-row')).toHaveLength(3)
    expect(screen.getByRole('tab', { name: /Due now/ }).textContent).toBe('Due now (3)')
    expect(screen.getByRole('heading', { name: /^Overdue/ }).querySelector('.orb-count-chip')!.textContent)
      .toBe('2 tasks')
  })

  it('caps the uncharted earlier doses at 12+ on the row', async () => {
    const b = busyBoard()
    b.patients[1].medications[0] = { ...b.patients[1].medications[0], missed_slots: 168 }
    getShiftBoard.mockResolvedValue(b)
    await mount()

    const row = screen.getByRole('button', { name: /Piperacillin-tazobactam/ })
    expect(row.getAttribute('aria-label')).toContain('12+ not charted')
    expect(row.textContent).toContain('12+ not charted')
    expect(row.textContent).not.toContain('168')
  })

  it('carries the category tag beside words that say the same thing', async () => {
    getShiftBoard.mockResolvedValue(busyBoard())
    const { container } = await mount()

    const secondary = container.querySelector('#nurse-panel-tasks .orb-row__secondary')!
    const tag = secondary.querySelector('.orb-tag')!
    expect(tag.className).toContain('orb-tag--obs')
    // The tag is never the only carrier of meaning (§9.6).
    expect(secondary.textContent).toContain('Observations')
  })
})

/* ════════════════════════════════════════════════════════════════════════
   THE STYLESHEET RULES THAT A RENDER TEST CANNOT SEE
   (vitest runs with css:false and jsdom computes no cascade)
   ════════════════════════════════════════════════════════════════════════ */

/* ════════════════════════════════════════════════════════════════════════
   THE VISUAL GATE'S HOOKS
   ════════════════════════════════════════════════════════════════════════ */

describe('the selectors the visual gate waits on', () => {
  /* The nurse home had never been captured in any theme, and the reason was
     exactly this class of bug: `tests/visual/driver.cjs` had one nurse
     scenario and it waited on `.patient-row` — Vigil's roster — while a nurse
     lands on `shift`. The wait could only ever time out, and a timed-out
     scenario is a silently ungated screen.

     So the contract is locked from BOTH ends. Each selector below must exist
     in the driver (or the gate has stopped looking for it) and must render
     here (or the gate is waiting for something that no longer paints). */
  const DRIVER = readFileSync(join(process.cwd(), 'tests/visual/driver.cjs'), 'utf8')

  /** Every nurse scenario's `waitFor`, pulled straight out of the driver. */
  const nurseWaits = () => {
    const out: [string, string][] = []
    const re = /name: '((?:nurse|night\/nurse|responsive\/nurse)[^']*)'[\s\S]{0,400}?waitFor: '([^']+)'/g
    for (const m of DRIVER.matchAll(re)) out.push([m[1], m[2]])
    return out
  }

  /** The three board shapes the scenarios drive, matching tests/visual/fixtures.cjs. */
  const STATES: [string, () => ShiftBoard | null][] = [
    ['populated', busyBoard],
    ['nothing due', quietBoard],
    ['no patients', () => board({ totals: { patients: 0, critical: 0, elevated: 0, obs_overdue: 0, meds_overdue: 0 } })],
    ['read failed', () => null],
  ]

  it('covers My Shift in all three themes and at bedside width', () => {
    // The spec's finding was not "one scenario is wrong" but "this screen has
    // never been captured, in any theme". Dark and light come from the driver's
    // default; night and the tablet viewport have to be asked for by name.
    const scenarios = DRIVER.split(/\{\s*\n?\s*(?=\/\*|name:)/)
      .filter(s => /name: '(?:nurse|night\/nurse|responsive\/nurse)/.test(s))
    expect(scenarios.length).toBeGreaterThan(0)

    const defaultThemed = scenarios.filter(s => !/themes:/.test(s))
    expect(defaultThemed.length, 'no My Shift scenario runs the default dark+light pass').toBeGreaterThan(0)

    const night = scenarios.filter(s => /themes: \['night'\]/.test(s))
    expect(night.length, 'My Shift is not captured in night — the theme it is read in at 03:00').toBeGreaterThan(0)

    const tablet = scenarios.filter(s => /viewport: \{ width: 768/.test(s))
    expect(tablet.length, 'My Shift is not captured at 768px, the bedside tablet').toBeGreaterThan(0)
  })

  it('waits only on selectors My Shift can actually produce', async () => {
    // Render every state once and keep what each one painted.
    const painted: Element[][] = []
    for (const [, fixture] of STATES) {
      const b = fixture()
      if (b) getShiftBoard.mockResolvedValue(b)
      else getShiftBoard.mockRejectedValue(new Error('Shift board unavailable (500)'))
      const { container, unmount } = await mount()
      painted.push([...container.querySelectorAll('*')])
      unmount()
    }

    const unreachable: string[] = []
    for (const [name, waitFor] of nurseWaits()) {
      // The panel wrapper is the shell's, not this component's.
      const selector = waitFor.replace(/^#panel-shift\s*/, '').trim()
      if (!selector || selector === '#panel-shift') continue
      const reachable = painted.some(nodes => nodes.some(el => el.matches(selector)))
      if (!reachable) unreachable.push(`${name} → ${waitFor}`)
    }
    // A waitFor that nothing can match does not fail the gate — it times the
    // scenario out, and a timed-out scenario is an ungated screen that still
    // reports a name. That is precisely how `nurse/vigil` waited on
    // `.patient-row` for months.
    expect(unreachable).toEqual([])
  })

  it('keeps the view-tab ids the driver clicks', async () => {
    getShiftBoard.mockResolvedValue(busyBoard())
    const { container } = await mount()
    for (const id of ['tasks', 'patients', 'handover', 'shift']) {
      expect(container.querySelector(`#nurse-tab-${id}`), id).not.toBeNull()
    }
    // openNurseView() builds them by template, so the prefix is the contract.
    expect(DRIVER).toContain('#nurse-tab-${viewId}')
  })
})

describe('NursePanel.css house rules', () => {
  const bare = NURSE_CSS.replace(/\/\*[\s\S]*?\*\//g, '')

  it('never expresses de-emphasis or state with opacity', () => {
    expect(bare).not.toMatch(/(^|[;{\s])opacity\s*:/)
  })

  it('moves nothing, ever — no lift, no scale, no reorder', () => {
    // A row that moves under a descending thumb causes a mis-tap, and a
    // mis-tapped task row on this product is a clinical error.
    expect(bare).not.toMatch(/(^|[;{\s])transform\s*:/)
    expect(bare).not.toMatch(/(^|[;{\s])order\s*:/)
  })

  it('keeps every shrunken control above the coarse-pointer floor', () => {
    // 24px is the pointer floor and 44px (--tap-min) is the touch floor. Any
    // rule that sets a smaller explicit min-height has to have a matching
    // coarse-pointer rule bringing it back, or it ships a target a gloved hand
    // cannot reliably hit on the bedside tablet this is mostly used on.
    const coarse = bare.match(/@media \(pointer: coarse\)\s*\{[\s\S]*?\n\}/g)?.join('\n') ?? ''

    const shrunken = bare
      .split('}')
      .filter(block => !/@media/.test(block) && /min-height:\s*\d+px/.test(block))
      .map(block => ({
        selector: block.split('{')[0].trim().replace(/\s+/g, ' '),
        px: parseInt(/min-height:\s*(\d+)px/.exec(block)![1], 10),
      }))
      // Non-interactive boxes are geometry, not targets.
      .filter(r => !/count-chip|__input|textarea/.test(r.selector))

    const unbumped = shrunken
      .filter(r => r.px < 44)
      .filter(r => {
        const key = r.selector.split(/[\s,]/).filter(Boolean).pop() ?? r.selector
        return !new RegExp(`${key.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}[^{]*\\{[^}]*min-height:\\s*var\\(--tap-min\\)`).test(coarse)
      })
      .map(r => `${r.selector} (${r.px}px)`)

    expect(unbumped).toEqual([])
  })

  it('clamps a section header to the same measure as the list it heads', () => {
    // .orb-list clamps itself; an unclamped header parks its count chip a
    // metre right of the rows it is counting on a wide monitor.
    const block = bare.match(/\n\.nurse-block \{([^}]*)\}/)?.[1] ?? ''
    expect(block).toMatch(/max-width:\s*var\(--home-col-max\)/)
  })

  it('has no hover-only affordance hiding a word a nurse needs', () => {
    // .handover__state used to be `display: none` on a phone, leaving the green
    // left rule as the only carrier of "handed over".
    expect(bare).not.toMatch(/\.handover__state\s*\{[^}]*display:\s*none/)
  })

  it('re-declares no shared primitive that belongs to the eager shell stylesheet', () => {
    for (const cls of ['.orb-row', '.orb-list', '.orb-state', '.orb-count-chip', '.news2']) {
      // A descendant tweak scoped under a nurse-owned class is fine
      // (".nurse-totals .orb-count-chip" only reaches chips on this home). A
      // selector that STARTS with the shared class re-defines it for both.
      const owned = bare
        .split('}')
        .map(block => block.split('{')[0])
        .flatMap(sel => sel.split(','))
        .map(sel => sel.trim())
        .filter(sel => new RegExp(`^\\${cls}(?![\\w-])`).test(sel))
      expect(owned, cls).toEqual([])
    }
  })
})
