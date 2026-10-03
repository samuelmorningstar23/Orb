/**
 * Ward Round — the panel's behaviour at the bedside.
 *
 * The unit tests next door (components/round/roundQueue.test.ts and
 * roundSummary.test.ts) prove the RULES. This file proves the SCREEN: that Orb
 * actually drives the sequence, that an interrupted round comes back, that the
 * round can be handed on, and — the part that matters most on a clinical
 * surface — that "loading", "nothing to report" and "we could not find out"
 * never look like each other.
 */
import { render, screen, waitFor, act, fireEvent, cleanup } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const card = (id: string, name: string, bed: string, extra: Record<string, unknown> = {}) => ({
  patient_id: id, name, mrn: `MRN-${id}`, bed, ward: 'GM', age: 62, sex: 'M',
  diagnosis: 'Community-acquired pneumonia', department: 'General Medicine',
  attending_doctor_name: 'Dr Iyer', on_scale_2: false, allergies: [], news2: null,
  last_obs_at: '2026-08-07T06:00:00Z', obs_interval_minutes: 240, obs_band: 'routine',
  obs_response: 'routine', obs_due_at: null, obs_minutes_until_due: 120,
  obs_overdue: false, obs_due_soon: false, medications: [], meds_overdue: 0,
  meds_due_soon: 0, ...extra,
})

// Deliberately out of walk order, and with a two-digit bed, so the panel has to
// sort GM-2 < GM-9 < GM-10 rather than sorting them as strings.
const PATIENTS = [
  card('P010', 'Sunita Devi', 'GM-10'),
  card('P002', 'Anita Rao', 'GM-2'),
  card('P009', 'Rahul Menon', 'GM-9'),
]

const BOARD = {
  generated_at: '2026-08-07T08:00:00Z',
  department: 'General Medicine',
  patients: PATIENTS,
  totals: { patients: 3, critical: 0, elevated: 0, obs_overdue: 0, meds_overdue: 0 },
}

const getShiftBoard = vi.fn(async () => BOARD)
const getPatients = vi.fn(async () => [] as unknown[])
// Declares the parameter it is actually called with. Without it the mock's type
// is `() => …` and the `getPatientStory(id as never)` call below is a type error
// — invisible while the gate ran `tsc --noEmit` against the solution-style root
// tsconfig, which typechecks nothing.
const getPatientStory = vi.fn(async (_id: string) => [] as unknown[])
const getSageInsights = vi.fn(async () => ({ insights: [] as unknown[] }))

vi.mock('../../api/nurse', () => ({ getShiftBoard: () => getShiftBoard() }))

vi.mock('../../api/client', () => ({
  getPatients: () => getPatients(),
  getPatientStory: (id: string) => getPatientStory(id as never),
  getSageInsights: () => getSageInsights(),
  getUser: () => ({ id: 'doc-1', name: 'Dr Iyer', role: 'doctor' }),
  addClinicalNote: vi.fn(async () => ({ id: 'n1' })),
  transcribeAudio: vi.fn(async () => ({ transcript: '', audio_path: '' })),
}))

// Sage's Tier 3 assessment. Mocked rather than left to the real client so the
// round's own behaviour is asserted instead of the network's: the round READS
// one per bed and ASKS for one bed ahead, and both must be provable.
const getReasonedAssessment = vi.fn(async (_id: string) => ({
  patient_id: _id, assessment: null as unknown, stale_after_hours: 12, plain_text: '',
}))
const prepareReasonedAssessment = vi.fn(async (_id: string) => ({
  status: 'preparing', assessment: null, stale_after_hours: 12,
  reason: null, message: 'Orb Sage is writing a reasoned assessment for this patient.',
}))
vi.mock('../../api/ipass', () => ({
  getReasonedAssessment: (id: string) => getReasonedAssessment(id),
  prepareReasonedAssessment: (id: string) => prepareReasonedAssessment(id),
}))

const SAGE_RUNTIME = {
  posture: null, micOwner: null,
  claimMic: () => true, releaseMic: () => {}, cloudSpeechAllowed: false,
}
vi.mock('../../contexts/SageRuntimeContext', () => ({ useSageRuntime: () => SAGE_RUNTIME }))

import RoundPanel from './RoundPanel'
import { announceDataChanged } from '../../contexts/PanelActiveContext'

const currentName = () =>
  (document.querySelector('.round-patient__name') as HTMLElement | null)?.textContent ?? null
const atBed = (name: string) => waitFor(() => expect(currentName()).toBe(name))
const bedOrder = () =>
  Array.from(document.querySelectorAll('.round-rail__bed')).map(n => n.textContent)

async function click(name: RegExp) {
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name }))
    await Promise.resolve()
  })
}

describe('Ward round panel', () => {
  beforeEach(() => {
    localStorage.clear()
    getShiftBoard.mockClear().mockResolvedValue(BOARD)
    getPatients.mockClear().mockResolvedValue([])
    getPatientStory.mockClear().mockResolvedValue([])
    getSageInsights.mockClear().mockResolvedValue({ insights: [] })
    getReasonedAssessment.mockClear().mockImplementation(async (id: string) => ({
      patient_id: id, assessment: null, stale_after_hours: 12, plain_text: '',
    }))
    prepareReasonedAssessment.mockClear().mockResolvedValue({
      status: 'preparing', assessment: null, stale_after_hours: 12,
      reason: null, message: 'Orb Sage is writing a reasoned assessment for this patient.',
    })
  })
  afterEach(cleanup)

  it('opens on the first bed of the physical walk, not of the payload', async () => {
    render(<RoundPanel />)
    await atBed('Anita Rao')
    // GM-2 before GM-9 before GM-10: a corridor, not a string sort.
    expect(bedOrder()).toEqual(['GM-2', 'GM-9', 'GM-10'])
  })

  it('adds a patient admitted from the shell without leaving the bed or reloading', async () => {
    render(<RoundPanel />)
    await atBed('Anita Rao')

    // Hold the refresh open: the round must stay on screen while it runs. A
    // loading screen here would unmount any dictation in progress.
    let release!: (b: typeof BOARD) => void
    getShiftBoard.mockImplementationOnce(() => new Promise(res => { release = res }))
    act(() => { announceDataChanged() })
    expect(currentName()).toBe('Anita Rao')
    expect(getShiftBoard).toHaveBeenCalledTimes(2)

    await act(async () => {
      release({ ...BOARD, patients: [...PATIENTS, card('P004', 'Kiran Shah', 'GM-4')] })
      await Promise.resolve()
    })
    await waitFor(() => expect(bedOrder()).toEqual(['GM-2', 'GM-4', 'GM-9', 'GM-10']))
    expect(currentName()).toBe('Anita Rao')
  })

  it('drives the sequence bed by bed as patients are marked seen', async () => {
    render(<RoundPanel />)
    await atBed('Anita Rao')

    await click(/Seen · next bed/i)
    await atBed('Rahul Menon')

    await click(/Seen · next bed/i)
    await atBed('Sunita Devi')

    expect(screen.getByRole('progressbar', { name: /2 of 3 patients seen/i })).toBeInTheDocument()
  })

  it('moves the round on when a patient is skipped, and comes back to them', async () => {
    render(<RoundPanel />)
    await atBed('Anita Rao')

    // Anita is in radiology. Skipping must advance, not stall.
    await click(/Skip for now/i)
    await atBed('Rahul Menon')

    await click(/Seen · next bed/i)
    await atBed('Sunita Devi')

    // Nothing pending is left, so the round returns to the deferred bed.
    await click(/Seen · next bed/i)
    await atBed('Anita Rao')

    expect(screen.getByText(/1 left/i)).toBeInTheDocument()
  })

  it('resumes an interrupted round instead of starting again', async () => {
    const first = render(<RoundPanel />)
    await atBed('Anita Rao')
    await click(/Seen · next bed/i)
    await atBed('Rahul Menon')
    first.unmount()

    // The tablet was locked, dropped, or the battery died. Same doctor, same day.
    render(<RoundPanel />)
    await atBed('Rahul Menon')
    expect(await screen.findByText(/Resumed your round from earlier today/i)).toBeInTheDocument()
    expect(screen.getByRole('progressbar', { name: /1 of 3 patients seen/i })).toBeInTheDocument()
  })

  it('does not restore another clinician’s round left on a shared tablet', async () => {
    // A colleague's round, written by the same code path under their user id.
    localStorage.setItem(
      'orb_round_v1:doc-OTHER:2026-08-07',
      JSON.stringify({
        userId: 'doc-OTHER', dateKey: '2026-08-07',
        cursor: 'P009', statuses: { P002: 'seen', P009: 'skipped' },
      }),
    )

    render(<RoundPanel />)
    await atBed('Anita Rao')

    expect(screen.getByRole('progressbar', { name: /0 of 3 patients seen/i })).toBeInTheDocument()
    // Erased from the device, not merely ignored: which patients a named doctor
    // visited is itself disclosure.
    expect(localStorage.getItem('orb_round_v1:doc-OTHER:2026-08-07')).toBeNull()
  })

  it('hands the round on when the last bed is marked seen', async () => {
    render(<RoundPanel />)
    await atBed('Anita Rao')
    await click(/Seen · next bed/i)
    await click(/Seen · next bed/i)
    await click(/Seen · next bed/i)

    // The round is finished — the doctor must be told, not left staring at the
    // last patient with a "Seen · next bed" button that does nothing.
    expect(await screen.findByText(/Round complete/i)).toBeInTheDocument()
    expect(screen.getByText(/Every patient on this ward was seen/i)).toBeInTheDocument()
  })

  it('ends the round the same way when the last bed is completed by saving a plan', async () => {
    render(<RoundPanel />)
    await atBed('Anita Rao')
    await click(/Seen · next bed/i)
    await atBed('Rahul Menon')
    await click(/Seen · next bed/i)
    await atBed('Sunita Devi')

    // A dictated plan on the LAST bed. Marking seen is blocked until it is saved.
    const box = screen.getByLabelText('Plan for today')
    await act(async () => {
      fireEvent.change(box, { target: { value: 'discharge tomorrow if afebrile' } })
      await Promise.resolve()
    })
    await click(/Seen · next bed/i)
    expect(await screen.findByText(/has not been saved/i)).toBeInTheDocument()

    // Saving and marking seen finishes the round, so it must finish the same way
    // marking the last bed seen directly does. Otherwise the doctor is parked on
    // a patient they have already completed, under a "Seen · next bed" button
    // that does nothing, and the one artefact the end of a round has to produce
    // — the hand-on list — is never shown.
    await click(/Save plan and mark seen/i)

    expect(await screen.findByText(/Round complete/i)).toBeInTheDocument()
    expect(screen.getByText(/Every patient on this ward was seen/i)).toBeInTheDocument()
  })

  it('lists who was not finished, and why, on the hand-on screen', async () => {
    render(<RoundPanel />)
    await atBed('Anita Rao')
    await click(/Skip for now/i)
    await atBed('Rahul Menon')

    await click(/Finish and hand over/i)
    expect(await screen.findByText(/Still to be seen/i)).toBeInTheDocument()
    expect(screen.getByText('Skipped')).toBeInTheDocument()
    expect(screen.getAllByText('Not reached').length).toBe(2)
  })

  // ── The three states that must never be confused ──

  it('shows a loading state that is not an empty ward', async () => {
    let release!: () => void
    getShiftBoard.mockImplementation(
      () => new Promise(res => { release = () => res(BOARD) }),
    )

    render(<RoundPanel />)
    const loading = await screen.findByRole('status')
    expect(loading).toHaveAttribute('aria-busy', 'true')
    expect(screen.getByText(/Loading the ward round/i)).toBeInTheDocument()
    // Crucially: it does not claim there are no patients.
    expect(screen.queryByText(/No patients to round on/i)).toBeNull()

    await act(async () => { release(); await Promise.resolve() })
    await atBed('Anita Rao')
  })

  it('says an empty ward is genuinely empty, and names the ward', async () => {
    getShiftBoard.mockResolvedValue({ ...BOARD, patients: [], totals: BOARD.totals })

    render(<RoundPanel />)
    expect(await screen.findByText(/No patients to round on/i)).toBeInTheDocument()
    expect(screen.getByText(/no admitted patients in General Medicine/i)).toBeInTheDocument()
    // The sentence that stops an empty screen reading as a clinical statement.
    expect(screen.getByText(/Nothing failed to load/i)).toBeInTheDocument()
  })

  it('says a failed load is a failure, not an all-clear', async () => {
    getShiftBoard.mockRejectedValue(new Error('appliance unreachable'))
    getPatients.mockRejectedValue(new Error('appliance unreachable'))

    render(<RoundPanel />)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/could not be loaded/i)
    expect(alert).toHaveTextContent(/appliance unreachable/i)
    expect(alert).toHaveTextContent(/not an all-clear/i)
    expect(screen.queryByText(/No patients to round on/i)).toBeNull()
    expect(screen.getByRole('button', { name: /Retry/i })).toBeInTheDocument()
  })

  it('runs a degraded round, saying what is missing, when one roster fails', async () => {
    // The ward board is the one carrying scores, meds and allergies.
    getShiftBoard.mockRejectedValue(new Error('board timeout'))
    getPatients.mockResolvedValue([
      {
        id: 'P002', name: 'Anita Rao', bed: 'GM-2', ward: 'GM', mrn: 'MRN-P002',
        age: 62, sex: 'F', diagnosis: 'Sepsis', admission_date: '2026-08-05T00:00:00Z',
        risk_score: 7, risk_level: 'high', risk_drivers: [], attending_doctor_name: 'Dr Iyer',
      },
    ])

    render(<RoundPanel />)
    await atBed('Anita Rao')

    // The round still runs — losing one request must not empty the ward.
    expect(screen.getByText(/Ward board unavailable/i)).toBeInTheDocument()
    expect(screen.getByText(/board timeout/i)).toBeInTheDocument()
    // And the allergy row must say UNKNOWN, never render as "no allergies".
    expect(screen.getByText(/Unknown, ward data unavailable/i)).toBeInTheDocument()
  })

  it('keeps the overnight column honest when only the story request fails', async () => {
    getPatientStory.mockRejectedValue(new Error('story unavailable'))

    render(<RoundPanel />)
    await atBed('Anita Rao')

    expect(await screen.findByText(/Overnight events unavailable/i)).toBeInTheDocument()
    // Not "nothing happened overnight".
    expect(screen.queryByText(/Nothing recorded overnight/i)).toBeNull()
  })

  // ── Tablet in one hand ──

  it('gives every interactive round control at least a 44px touch target', () => {
    // jsdom does no layout, so a measured height here would be a fake. Assert
    // the contract where it actually lives — the stylesheet — because the
    // failure this guards against (a control quietly styled at 32px) is
    // invisible on a desktop and only bites a gloved thumb on a ward tablet.
    const css = readFileSync(
      resolve(process.cwd(), 'src/components/round/Round.css'), 'utf8',
    )
    const TAP_MIN = 44
    const offenders: string[] = []

    for (const [, selector, body] of css.matchAll(/([^{}]+)\{([^}]*)\}/g)) {
      const sel = selector.trim()
      // Interactive controls only: buttons, the queue rows, the mic.
      if (!/\.round-(btn|mic|rail__item|handoff__row)\b/.test(sel)) continue
      if (/:(hover|focus|active|disabled)/.test(sel)) continue
      const declared = /min-height:\s*([^;]+);/.exec(body)?.[1]?.trim()
      if (!declared) continue
      const px = declared === 'var(--tap-min)' ? TAP_MIN : parseFloat(declared)
      if (Number.isFinite(px) && px < TAP_MIN) offenders.push(`${sel} → ${declared}`)
    }

    expect(offenders).toEqual([])
  })

  it('advances the round from the keyboard without touching the screen', async () => {
    render(<RoundPanel />)
    await atBed('Anita Rao')

    const panel = document.querySelector('.round-panel') as HTMLElement
    await act(async () => { fireEvent.keyDown(panel, { key: 's' }); await Promise.resolve() })
    await atBed('Rahul Menon')

    await act(async () => { fireEvent.keyDown(panel, { key: 'k' }); await Promise.resolve() })
    await atBed('Sunita Devi')

    await act(async () => { fireEvent.keyDown(panel, { key: 'p' }); await Promise.resolve() })
    await atBed('Rahul Menon')
  })

  it('does not fire round shortcuts while the plan is being typed', async () => {
    render(<RoundPanel />)
    await atBed('Anita Rao')

    const box = screen.getByLabelText('Plan for today')
    await act(async () => {
      fireEvent.change(box, { target: { value: 'stop septic screen' } })
      fireEvent.keyDown(box, { key: 's' })
      await Promise.resolve()
    })
    // Still at Anita: the "s" was a letter in a plan, not a command.
    expect(currentName()).toBe('Anita Rao')
  })
})

/* ──────────────────────────────────────────────────────────────────────────
 * D3 ON THE ROUND — Tier 3 was served and never asked for.
 *
 * The relocation put the 46-second assessment on the handover and the ward
 * round, "because the round can prepare the next patient while the clinician is
 * still at this one". The round did neither: `grep -rn reasoned src/` returned
 * nothing at all. These pin both halves of the split that makes the tier
 * affordable — ASK for the bed ahead, READ the bed you are at — plus the rule
 * that absence renders as nothing.
 * ────────────────────────────────────────────────────────────────────────── */
const ASSESS_HEADING = 'SUPPLEMENT — AI-DRAFTED ASSESSMENT (NOT WRITTEN BY A CLINICIAN)'

const ROUND_ASSESSMENT = {
  available: true,
  ai_drafted: true,
  authored_by: 'Orb Sage (artificial intelligence)',
  label: 'AI-drafted by Orb Sage, not written by a clinician',
  heading: ASSESS_HEADING,
  disclaimer:
    'Nothing in this section has been ordered, charted or administered. ' +
    'It is machine-generated decision support and carries no clinical authority.',
  assessment_id: 'assess-round-1',
  reused: true,
  reuse_basis: 'drafted_for_review',
  reuse_basis_display: 'when this patient was prepared for review',
  drafted_at: '2026-08-07T07:40:00+00:00',
  drafted_at_display: '7 Aug, 07:40',
  age_minutes: 20,
  age_display: '20 minutes ago',
  stale_after_hours: 12,
  state: 'ready',
  state_display: 'not yet reviewed by a clinician',
  endorsed_by: null,
  endorsed_at: null,
  news2_at_drafting: 7,
  news2_level_at_drafting: 'critical',
  news2_context: 'NEWS2 7 (critical) as scored when this was written',
  whats_happening: 'Persistent tachypnoea despite antibiotics.',
  considerations: [{ step: 'Repeat the chest film', why: 'Effusion not excluded', orb_can_act: false }],
  watch_items: ['Whether the patient can speak in full sentences'],
  evidence: [],
  citation_basis: 'no_guideline',
  citation_display: 'no guideline in Orb’s corpus',
  confidence: 'moderate',
  confidence_display: 'moderate',
  model: 'medgemma:27b',
}

describe('Sage’s reasoned assessment on the round', () => {
  beforeEach(() => {
    localStorage.clear()
    getShiftBoard.mockClear().mockResolvedValue(BOARD)
    getPatients.mockClear().mockResolvedValue([])
    getPatientStory.mockClear().mockResolvedValue([])
    getSageInsights.mockClear().mockResolvedValue({ insights: [] })
    getReasonedAssessment.mockClear().mockImplementation(async (id: string) => ({
      patient_id: id, assessment: null, stale_after_hours: 12, plain_text: '',
    }))
    prepareReasonedAssessment.mockClear().mockResolvedValue({
      status: 'preparing', assessment: null, stale_after_hours: 12,
      reason: null, message: 'writing',
    })
  })
  afterEach(cleanup)

  it('reads the assessment for the bed it is at', async () => {
    render(<RoundPanel />)
    await atBed('Anita Rao')
    await waitFor(() => expect(getReasonedAssessment).toHaveBeenCalledWith('P002'))
  })

  it('asks for the NEXT bed while the doctor is still at this one', async () => {
    // This is the entire reason a 46-second generation is affordable here: the
    // asking and the reading are different moments.
    render(<RoundPanel />)
    await atBed('Anita Rao')
    await waitFor(() => {
      const asked = prepareReasonedAssessment.mock.calls.map(c => c[0])
      expect(asked).toContain('P002')   // this bed — or the first patient of
      expect(asked).toContain('P009')   // every round would be the one nobody asked about
    })
  })

  it('shows a ready assessment at the bedside with the server’s labelling verbatim', async () => {
    getReasonedAssessment.mockImplementation(async (id: string) => ({
      patient_id: id,
      assessment: id === 'P002' ? ROUND_ASSESSMENT : null,
      stale_after_hours: 12,
      plain_text: '',
    }))
    render(<RoundPanel />)
    await atBed('Anita Rao')

    const block = await screen.findByLabelText(ASSESS_HEADING)
    expect(block.textContent).toContain(ASSESS_HEADING)
    expect(block.textContent).toContain('AI-drafted by Orb Sage, not written by a clinician')
    expect(block.textContent).toContain('carries no clinical authority')
    expect(block.textContent).toContain('Persistent tachypnoea despite antibiotics.')
  })

  it('renders nothing at all for a bed with no assessment', async () => {
    render(<RoundPanel />)
    await atBed('Anita Rao')
    await waitFor(() => expect(getReasonedAssessment).toHaveBeenCalledWith('P002'))

    expect(document.querySelector('.orb-assess')).toBeNull()
    const screenText = (document.querySelector('.round-summary') as HTMLElement).textContent!
    expect(screenText).not.toContain('SUPPLEMENT')
    expect(screenText).not.toMatch(/writing an assessment|assessment unavailable/i)
  })

  it('does not carry one patient’s assessment to the next bed', async () => {
    // The sharpest failure this surface could have: a machine opinion about
    // Anita read at Rahul's bed.
    getReasonedAssessment.mockImplementation(async (id: string) => ({
      patient_id: id,
      assessment: id === 'P002' ? ROUND_ASSESSMENT : null,
      stale_after_hours: 12,
      plain_text: '',
    }))
    render(<RoundPanel />)
    await atBed('Anita Rao')
    await screen.findByLabelText(ASSESS_HEADING)

    await click(/Seen · next bed/)
    await atBed('Rahul Menon')
    await waitFor(() => expect(document.querySelector('.orb-assess')).toBeNull())
  })

  it('a read that fails leaves the round intact and says nothing about it', async () => {
    getReasonedAssessment.mockRejectedValue(new Error('Not authorized'))
    render(<RoundPanel />)
    await atBed('Anita Rao')

    await waitFor(() => expect(getReasonedAssessment).toHaveBeenCalled())
    expect(document.querySelector('.orb-assess')).toBeNull()
    expect(document.body.textContent).not.toMatch(/Not authorized/i)
    expect(currentName()).toBe('Anita Rao')
  })
})
