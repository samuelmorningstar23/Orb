/**
 * THE DOCTOR HOME IS A READ-AND-NAVIGATE SCREEN. THAT IS WHY IT IS SAFE.
 *
 * "Today" is a brand-new landing surface put in front of doctors inside the
 * pilot window, so what has to be pinned is not that it looks right — it is
 * that it cannot DO anything. Four properties, each of which would be a real
 * incident rather than a cosmetic regression:
 *
 *   1. READ-ONLY. Every request the panel issues is a GET. A write path added
 *      here would be a way to change a patient record that no audited,
 *      permission-gated panel authored — which is exactly the thing this screen
 *      was designed to have none of. Asserted against the real api/client, at
 *      the transport: the global fetch is stubbed and every call's METHOD is
 *      checked, so a write introduced through any helper is caught even if the
 *      helper is one nobody thought to mock.
 *
 *   2. THE PERMISSION GATE IS A PERMISSION, NOT A ROLE LIST. The end-of-shift
 *      block renders for the roles the SERVER grants `outcome.record` and for
 *      no others, and denial produces ABSENCE — never a disabled control that
 *      exists only to 403.
 *
 *   3. THE BLINDING HOLDS. A recorder who can watch the scorecard move as they
 *      enter outcomes has been handed an incentive to enter them differently,
 *      so the capture path reads no scorecard endpoint and shows no rate or
 *      metric on screen.
 *
 *   4. DEEP LINKS GO WHERE THEY SAY. Every row on this page is a promise about
 *      where pressing it lands the clinician; a row that navigates somewhere
 *      the doctor cannot act is the same defect class as a button that 403s.
 *
 * The state tests are here for the rule that outranks all of them: a section
 * that FAILED must never render as empty, because "nothing to report" and "we
 * could not find out" look identical when both render as nothing.
 */
import { render, screen, waitFor, within, act, fireEvent } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ROUND_STATE_PREFIX, todayKey } from '../../components/round/roundQueue'
import { takePendingSageQuestion } from '../../lib/orbBus'

const WARD = 'General Medicine'
const DOCTOR = { id: 'DOC001', name: 'Dr Iyer', role: 'doctor', department: WARD }

/* ── Fixtures ─────────────────────────────────────────────────────────────── */

function news2(raw: number, level: 'critical' | 'elevated' | 'stable', drivers: string[] = []) {
  return {
    score: raw / 20, level, raw_news2: raw, single_param_3: false,
    drivers, breakdown: [], scoring_model: 'NEWS2', spo2_scale: 1 as const,
  }
}

function card(id: string, name: string, bed: string, n: ReturnType<typeof news2> | null, dx: string) {
  return {
    patient_id: id, name, mrn: `MRN-${id}`, bed, ward: WARD, age: 61, sex: 'F',
    diagnosis: dx, department: WARD, attending_doctor_name: 'Dr Iyer',
    on_scale_2: false, allergies: [], news2: n,
    last_obs_at: '2026-08-12T08:40:00+00:00', obs_interval_minutes: 60,
    obs_band: 'stable', obs_response: '', obs_due_at: null,
    obs_minutes_until_due: 40, obs_overdue: false, obs_due_soon: false,
    medications: [], meds_overdue: 0, meds_due_soon: 0,
  }
}

/** Three deteriorating, five stable — a busy but not catastrophic ward. */
const PATIENTS = [
  card('P-04', 'Anita Rao', 'GM-04', news2(9, 'critical', ['High RR (26)', 'Low SpO₂ (91%)']), 'Sepsis'),
  card('P-11', 'Vikas Shah', 'GM-11', news2(7, 'elevated', ['Tachycardia (HR 118)']), 'Pneumonia'),
  card('P-02', 'Sara Iqbal', 'GM-02', news2(6, 'elevated', ['New O₂ requirement']), 'CCF'),
  card('P-07', 'Deepa Menon', 'GM-07', news2(1, 'stable'), 'Cellulitis'),
  card('P-08', 'Ravi Nair', 'GM-08', news2(0, 'stable'), 'COPD'),
  card('P-09', 'Meera Das', 'GM-09', news2(2, 'stable'), 'UTI'),
  card('P-10', 'Arun Pillai', 'GM-10', news2(1, 'stable'), 'Fracture'),
  card('P-12', 'Latha Menon', 'GM-12', null, 'Observation'),
]

const BOARD = {
  generated_at: new Date().toISOString(),
  department: WARD,
  patients: PATIENTS,
  totals: { patients: PATIENTS.length, critical: 1, elevated: 2, obs_overdue: 0, meds_overdue: 0 },
}

const UNSEEN = { counts: { 'P-04': 3, 'P-11': 2, 'P-02': 2 } }

const NOTES = [
  { id: 1, status: 'draft', patient_id: 'P-04' },
  { id: 2, status: 'draft', patient_id: 'P-11' },
  { id: 3, status: 'draft', patient_id: 'P-02' },
  { id: 4, status: 'signed', patient_id: 'P-07' },
]

const ALERTS = [{ id: 91, channel_id: 'c1', content: '{"is_structured":true}', message_type: 'alert' }]

const SHIFT_STATUS = {
  generated_at: new Date().toISOString(),
  status_available: true,
  shifts: [
    {
      shift: 'night', period_start: '2026-08-11T23:00:00.000+00:00',
      period_end: '2026-08-12T07:00:00.000+00:00', label: '23:00–07:00', is_current: false,
      wards: [{
        ward: WARD, confirmed: false, confirmed_at: null, confirmed_by_role: null,
        events_recorded: 0, status: 'outstanding',
      }],
    },
    {
      shift: 'morning', period_start: '2026-08-12T07:00:00.000+00:00',
      period_end: '2026-08-12T15:00:00.000+00:00', label: '07:00–15:00', is_current: true,
      wards: [{
        ward: WARD, confirmed: true, confirmed_at: '2026-08-12T07:05:00.000+00:00',
        confirmed_by_role: 'nurse', events_recorded: 0, status: 'confirmed',
      }],
    },
  ],
  max_backdate_hours: 72,
  event_types: ['icu_transfer', 'code_blue'],
  sources: ['witnessed', 'chart_review', 'handover'],
  recognition: ['clinician_independent', 'prompted_by_orb', 'unknown'],
  why: 'A ward-shift with neither a recorded event nor a confirmation is uncovered time.',
}

/* ── Transport ────────────────────────────────────────────────────────────── */

interface Call { url: string; method: string }
const calls: Call[] = []

/** Per-path overrides for the failure tests. `null` = make this route fail. */
let routes: Record<string, unknown | null> = {}

function bodyFor(url: string): unknown | null | undefined {
  for (const [fragment, body] of Object.entries(routes)) {
    if (url.includes(fragment)) return body
  }
  return undefined
}

function stubFetch() {
  vi.stubGlobal('fetch', vi.fn(async (url: unknown, init: RequestInit = {}) => {
    const u = String(url)
    calls.push({ url: u, method: String(init.method ?? 'GET').toUpperCase() })
    const body = bodyFor(u)
    if (body === null || body === undefined) {
      return {
        ok: false, status: 500, headers: { get: () => null },
        json: async () => ({ detail: `stub: no route for ${u}` }),
      }
    }
    return { ok: true, status: 200, headers: { get: () => null }, json: async () => body }
  }))
}

const DEFAULT_ROUTES = {
  '/nurse/board': BOARD,
  '/relay/alerts': ALERTS,
  '/scribe/notes': NOTES,
  '/chart/unseen': UNSEEN,
  '/outcome/shift-status': SHIFT_STATUS,
}

/* ── Bus recording ────────────────────────────────────────────────────────── */

const bus: { type: string; detail: unknown }[] = []
const busTypes = ['orb:goto-tab', 'orb:open-patient', 'orb:sage-ask']
const busListener = (e: Event) => bus.push({ type: e.type, detail: (e as CustomEvent).detail })

/* ── Harness ──────────────────────────────────────────────────────────────── */

import DoctorHomePanel from './DoctorHomePanel'

function signIn(role: string | null, department: string | null = WARD) {
  localStorage.setItem('orb_user', JSON.stringify({ ...DOCTOR, role, department }))
}

/** Put a half-finished ward round on the device, the way RoundPanel would. */
function startRound(seen: string[], skipped: string[] = []) {
  const statuses: Record<string, string> = {}
  for (const id of seen) statuses[id] = 'seen'
  for (const id of skipped) statuses[id] = 'skipped'
  localStorage.setItem(
    `${ROUND_STATE_PREFIX}${DOCTOR.id}:${todayKey()}`,
    JSON.stringify({ userId: DOCTOR.id, dateKey: todayKey(), cursor: null, statuses }),
  )
}

/** Render and wait for the first load of every section to settle. */
async function home(role: string | null = 'doctor') {
  signIn(role)
  const view = render(<DoctorHomePanel />)
  await screen.findByRole('heading', { name: 'Today', level: 1 })
  await act(async () => { await Promise.resolve(); await Promise.resolve() })
  return view
}

function section(labelText: RegExp | string): HTMLElement {
  const heading = screen.getByRole('heading', { name: labelText, level: 2 })
  const el = heading.closest('section')
  if (!el) throw new Error(`no section for ${String(labelText)}`)
  return el as HTMLElement
}

beforeEach(() => {
  localStorage.clear()
  calls.length = 0
  bus.length = 0
  routes = { ...DEFAULT_ROUTES }
  stubFetch()
  for (const t of busTypes) window.addEventListener(t, busListener)
})

afterEach(() => {
  for (const t of busTypes) window.removeEventListener(t, busListener)
  vi.unstubAllGlobals()
})

/* ══════════════════════════════════════════════════════════════════════════ */

describe('Today — the four sections compose from data that already exists', () => {
  it('lists the deteriorating patients, sickest first, with their NEWS2', async () => {
    await home()
    const block = section(/^Deteriorating/)
    const rows = within(block).getAllByRole('button')

    // Only the patients scoring above threshold, in band-then-score order —
    // never the whole roster, which would be the roster with extra steps.
    expect(rows[0]).toHaveAccessibleName(/Anita Rao, bed GM-04, NEWS2 9 \(critical\)/)
    expect(rows[1]).toHaveAccessibleName(/Vikas Shah, bed GM-11, NEWS2 7 \(elevated\)/)
    expect(rows[2]).toHaveAccessibleName(/Sara Iqbal, bed GM-02, NEWS2 6 \(elevated\)/)
    expect(rows[3]).toHaveAccessibleName(/See all 8 patients/)
    expect(within(block).queryByText(/Deepa Menon/)).toBeNull()

    // Bed first, because a doctor is looking for a room, not a name.
    expect(within(block).getByText('GM-04 · Anita Rao')).toBeTruthy()
    // Diagnosis plus the NEWS2 drivers, which is the server's own attribution.
    expect(within(block).getByText('Sepsis · High RR (26) · Low SpO₂ (91%)')).toBeTruthy()

    // The count chip carries the number and a screen-reader unit; the LABEL
    // never takes a status colour.
    expect(within(block).getByRole('heading', { level: 2 })).toHaveTextContent('Deteriorating3 patients')
  })

  it('lists what is waiting on this doctor: signatures, alerts, an unfinished round', async () => {
    startRound(['P-04', 'P-11', 'P-02', 'P-07', 'P-08'], ['P-09'])
    await home()
    const block = section(/^Pending my decision/)

    expect(within(block).getByText('3 notes awaiting your signature')).toBeTruthy()
    expect(within(block).getByText('1 safety alert unresolved')).toBeTruthy()
    // Computed with the round's OWN functions over its own persisted marks.
    expect(within(block).getByText('Ward round: 5 of 8 patients seen')).toBeTruthy()
    expect(within(block).getByText('3 still to see · 1 skipped')).toBeTruthy()
    expect(within(block).getByRole('heading', { level: 2 })).toHaveTextContent('5 items')
  })

  it('aggregates the per-bed unseen counts, neutrally', async () => {
    await home()
    const block = section(/^New results/)

    expect(within(block).getByText('GM-04 Anita Rao')).toBeTruthy()
    expect(within(block).getByRole('heading', { level: 2 })).toHaveTextContent('New results7 results')
    // A fact list, not an alarm: no status rule on any row in this section.
    for (const row of within(block).getAllByRole('button')) {
      expect(row.className).not.toMatch(/orb-row--(overdue|soon)/)
    }
  })

  it('does not animate a clinical number into place', async () => {
    await home()
    // The NEWS2 badge paints its integer; nothing on this page counts up.
    const badge = screen.getAllByRole('img', { name: /NEWS2 9 \(critical\)/ })[0]
    expect(badge.textContent).toBe('9')
  })
})

describe('deep links land where the row says they will', () => {
  it('opens the patient chart from a deteriorating row', async () => {
    await home()
    fireEvent.click(within(section(/^Deteriorating/)).getAllByRole('button')[0])
    expect(bus).toEqual([
      { type: 'orb:goto-tab', detail: { tab: 'vigil' } },
      { type: 'orb:open-patient', detail: { patientId: 'P-04', name: 'Anita Rao' } },
    ])
  })

  it('sends the signature queue to Scribe and the ward round to Round', async () => {
    startRound(['P-04'])
    await home()
    const block = section(/^Pending my decision/)

    fireEvent.click(within(block).getByRole('button', { name: /awaiting your signature/ }))
    expect(bus.at(-1)).toEqual({ type: 'orb:goto-tab', detail: { tab: 'scribe' } })

    fireEvent.click(within(block).getByRole('button', { name: /resume the round/ }))
    expect(bus.at(-1)).toEqual({ type: 'orb:goto-tab', detail: { tab: 'round' } })
  })

  it('sends an unresolved safety alert to Messages, where it can be resolved', async () => {
    await home()
    fireEvent.click(within(section(/^Pending my decision/))
      .getByRole('button', { name: /safety alert unresolved/ }))
    // NOT the patient board: these are Relay's targeted alerts and Relay is the
    // only surface that can acknowledge or escalate one. A row that navigates
    // somewhere the doctor cannot act is the defect this design exists to kill.
    expect(bus.at(-1)).toEqual({ type: 'orb:goto-tab', detail: { tab: 'relay' } })
  })

  it('carries the Sage question into the Sage tab instead of asking from here', async () => {
    await home()
    const input = screen.getByLabelText('Ask Sage about this ward')
    fireEvent.change(input, { target: { value: 'Who is most likely to need ICU tonight?' } })
    fireEvent.click(screen.getByRole('button', { name: 'Ask Sage' }))

    expect(bus).toEqual([
      { type: 'orb:goto-tab', detail: { tab: 'sage' } },
      { type: 'orb:sage-ask', detail: { question: 'Who is most likely to need ICU tonight?' } },
    ])
    // The composer navigates. It never talks to a model itself.
    expect(calls.some(c => /\/sage/.test(c.url))).toBe(false)
  })

  it('LATCHES the question for a Sage panel that has not mounted yet', async () => {
    await home()
    fireEvent.change(screen.getByLabelText('Ask Sage about this ward'),
      { target: { value: 'Any new sepsis on the ward?' } })
    fireEvent.click(screen.getByRole('button', { name: 'Ask Sage' }))

    // SagePanel is a lazy chunk that has not been fetched, let alone mounted,
    // at the instant the event fires — a listener that does not exist cannot
    // hear it. Without the latch the first ask of every session would vanish
    // and the doctor would land on an empty composer with no idea why.
    expect(takePendingSageQuestion()).toBe('Any new sepsis on the ward?')
    // Read once: whichever path delivers it clears it.
    expect(takePendingSageQuestion()).toBeNull()
  })

  it('submits on Enter and keeps Shift+Enter for a newline', async () => {
    await home()
    const input = screen.getByLabelText('Ask Sage about this ward')

    fireEvent.change(input, { target: { value: 'first line' } })
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true })
    expect(bus).toHaveLength(0)

    fireEvent.keyDown(input, { key: 'Enter' })
    expect(bus.at(-1)).toEqual({ type: 'orb:sage-ask', detail: { question: 'first line' } })
  })
})

describe('the end-of-shift block is gated on the permission, not on a role name', () => {
  // Exactly the roles server/permissions.py grants `outcome.record`.
  it.each(['doctor', 'clinician', 'nurse', 'admin'])('renders for %s', async role => {
    await home(role)
    expect(screen.getByRole('heading', { name: /^End of shift/, level: 2 })).toBeTruthy()
  })

  it.each(['pharmacist', 'patient', 'surgeon'])(
    'is ABSENT for %s — never a disabled control', async role => {
      await home(role)
      expect(screen.queryByRole('heading', { name: /^End of shift/, level: 2 })).toBeNull()
      // Absence, not a lock icon or an explanatory grey card: a greyed-out
      // block is a promise being withheld, and a 403-on-click is the defect.
      expect(screen.queryByText(/end-of-shift outcomes/i)).toBeNull()
      expect(calls.some(c => c.url.includes('/outcome/shift-status'))).toBe(false)
    })

  it('is absent when the session carries no role at all', async () => {
    await home(null)
    expect(screen.queryByRole('heading', { name: /^End of shift/, level: 2 })).toBeNull()
  })

  it('names the shift that is owed, and opens the shared capture sheet', async () => {
    await home()
    const block = section(/^End of shift/)
    expect(within(block).getByText('Confirm the night shift (23:00–07:00)')).toBeTruthy()
    expect(within(block).getByRole('heading', { level: 2 })).toHaveTextContent('1 shift to confirm')
    // The explanation wraps in full, and the row no longer repeats the count as "owed".
    const why = within(block).getByText('No event or confirmation was recorded for this shift.')
    expect(why.className).toContain('orb-row__secondary--wrap')
    expect(block.textContent).not.toMatch(/owed/i)

    fireEvent.click(within(block).getByRole('button'))
    const dialog = await screen.findByRole('dialog', { name: /end-of-shift outcomes/i })
    // The nurse's own sheet, composed rather than copied: its ward field, its
    // shift window, its two writes.
    await waitFor(() => expect(dialog.querySelector('#shift-capture-ward')).toBeTruthy())
    expect((dialog.querySelector('#shift-capture-ward') as HTMLInputElement).value).toBe(WARD)
  })

  it('BLINDING: no scorecard is read, and no rate or metric reaches the screen', async () => {
    await home()
    fireEvent.click(within(section(/^End of shift/)).getByRole('button'))
    const dialog = await screen.findByRole('dialog', { name: /end-of-shift outcomes/i })
    await waitFor(() =>
      expect(calls.some(c => c.url.includes('/outcome/shift-status'))).toBe(true))

    // Not by any transport, from any part of this screen.
    for (const c of calls) expect(c.url).not.toMatch(/scorecard|adjudicat|export/i)
    // And every shadow call is a capture endpoint.
    for (const c of calls.filter(c => c.url.includes('/admin/shadow'))) {
      expect(c.url).toMatch(/\/admin\/shadow\/outcome/)
    }
    // The home block itself shows owed-shift state only — no count of events,
    // no rate, nothing about how Orb is scoring.
    expect(section(/^End of shift/).textContent).not.toMatch(/%|rate|events recorded/i)
    expect(dialog.textContent).not.toMatch(/false[- ]alert|sensitivity|specificity|lead time/i)
  })
})

describe('READ-ONLY: the panel cannot change anything', () => {
  it('issues nothing but GETs, across every section and the capture sheet', async () => {
    startRound(['P-04'])
    await home()
    // Exercise every control that is not itself a write: refresh, the Sage
    // composer, and opening the capture modal.
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    fireEvent.click(within(section(/^End of shift/)).getByRole('button'))
    await screen.findByRole('dialog', { name: /end-of-shift outcomes/i })
    await act(async () => { await Promise.resolve(); await Promise.resolve() })

    expect(calls.length).toBeGreaterThan(0)
    const writes = calls.filter(c => c.method !== 'GET')
    expect(writes).toEqual([])
  })

  it('reaches only the five read endpoints it composes', async () => {
    await home()
    const paths = new Set(calls.map(c => c.url.replace(/^https?:\/\/[^/]+/, '').split('?')[0]))
    expect([...paths].sort()).toEqual([
      '/api/admin/shadow/outcome/shift-status',
      '/api/chart/unseen',
      '/api/nurse/board',
      '/api/relay/alerts',
      '/api/scribe/notes',
    ])
  })

  it('does not put /chart/unseen on the background poll', async () => {
    vi.useFakeTimers()
    try {
      signIn('doctor')
      render(<DoctorHomePanel />)
      await act(async () => { await Promise.resolve(); await Promise.resolve() })
      const before = calls.filter(c => c.url.includes('/chart/unseen')).length
      await act(async () => { vi.advanceTimersByTime(180_000) })
      // The board keeps refreshing; the audited per-bed PHI read does not.
      // A per-tick write into encrypted SQLite is the shape behind the July
      // event-loop stalls, which is why VigilPanel keeps it user-driven too.
      expect(calls.filter(c => c.url.includes('/nurse/board')).length).toBeGreaterThan(1)
      expect(calls.filter(c => c.url.includes('/chart/unseen')).length).toBe(before)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('every state is designed, and failure never looks like calm', () => {
  it('skeletons the sections on first load, with the row geometry they will take', async () => {
    signIn('doctor')
    // Nothing resolves: the panel is caught mid-first-load.
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))
    render(<DoctorHomePanel />)

    const loading = await screen.findAllByRole('status', { busy: true })
    expect(loading.length).toBeGreaterThan(0)
    expect(screen.getByText('Loading deteriorating patients')).toBeTruthy()
    // Skeleton rows ARE .orb-row, so the loading and loaded geometries cannot
    // drift apart and nothing jumps when the data lands.
    expect(loading[0].querySelectorAll('li.orb-row').length).toBe(3)
    expect(loading[0].querySelector('.orb-skeleton__bar')).toBeTruthy()
  })

  it('says what it searched and when, on a quiet ward', async () => {
    routes['/nurse/board'] = {
      ...BOARD,
      patients: PATIENTS.filter(p => p.news2?.level === 'stable'),
      totals: { ...BOARD.totals, patients: 4, critical: 0, elevated: 0 },
    }
    routes['/chart/unseen'] = { counts: {} }
    routes['/scribe/notes'] = []
    routes['/relay/alerts'] = []
    await home()

    // "Nothing here" is a claim; naming the cohort and the clock makes it
    // evidence, which is the difference between a tired reader trusting it
    // and not.
    expect(screen.getByText('No patient is scoring above threshold.')).toBeTruthy()
    expect(screen.getByText(/All 4 patients on General Medicine are at NEWS2 4 or below/)).toBeTruthy()
    expect(screen.getByText('Nothing is waiting on you.')).toBeTruthy()
    expect(screen.getByText(/0 notes to sign · 0 unresolved safety alerts/)).toBeTruthy()
    expect(screen.getByText('No new results since you last looked.')).toBeTruthy()
    // On a quiet ward, no row on the page carries a saturated status rule.
    expect(document.querySelectorAll('.orb-row--overdue, .orb-row--soon').length).toBe(0)
  })

  it('renders a failed section as a FAILURE, never as an all-clear', async () => {
    routes['/chart/unseen'] = null
    await home()

    const block = section(/^New results/)
    const alert = within(block).getByRole('alert')
    expect(alert).toHaveTextContent("Couldn't load new results")
    expect(alert).toHaveTextContent(/This is a display failure, not an all-clear/)
    // The section that failed does not also claim there is nothing to see.
    expect(within(block).queryByText('No new results since you last looked.')).toBeNull()
    // …and the sections that succeeded are untouched. States are per section.
    expect(within(section(/^Deteriorating/)).getByText('GM-04 · Anita Rao')).toBeTruthy()
  })

  it('a runtime 403 on a permitted read is an error, not silence', async () => {
    signIn('doctor')
    vi.stubGlobal('fetch', vi.fn(async (url: unknown, init: RequestInit = {}) => {
      const u = String(url)
      calls.push({ url: u, method: String(init.method ?? 'GET').toUpperCase() })
      if (u.includes('/outcome/shift-status')) {
        return {
          ok: false, status: 403, headers: { get: () => null },
          json: async () => ({ detail: { message: 'Insufficient permissions' } }),
        }
      }
      const body = bodyFor(u)
      return { ok: true, status: 200, headers: { get: () => null }, json: async () => body ?? {} }
    }))
    render(<DoctorHomePanel />)
    await screen.findByRole('heading', { name: 'Today', level: 1 })

    // The client mirror said yes and the server said no: that drift must be
    // visible, because a silently empty section after a refusal is
    // indistinguishable from a ward that owes nothing.
    const block = await waitFor(() => section(/^End of shift/))
    expect(within(block).getByRole('alert')).toHaveTextContent('Orb refused this read for your account')
  })

  it('ages its own context line rather than crying wolf with a banner', async () => {
    routes['/nurse/board'] = {
      ...BOARD,
      generated_at: new Date(Date.now() - 14 * 60_000).toISOString(),
    }
    await home()
    const context = document.querySelector('.orb-home__context') as HTMLElement
    expect(context.className).toMatch(/orb-home__context--very-stale/)
    expect(context).toHaveTextContent(/14 min ago/)
    // The clock time never disappears — a clinician compares it to her watch.
    expect(context.querySelector('.orb-home__clock')?.textContent).toBeTruthy()
    // No offline banner: this is a live view that has stopped refreshing, which
    // is a different thing from a cached one.
    expect(screen.queryByText(/showing the ward board saved at/i)).toBeNull()
  })
})
