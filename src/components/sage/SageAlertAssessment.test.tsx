/**
 * THE DRAFTED ASSESSMENT, AS A CLINICIAN SEES IT.
 *
 * These are not rendering tests. Each one is a property the regulatory posture
 * or the pilot dataset depends on, and each one is written so that it FAILS if
 * the property is removed:
 *
 *   1. NOTHING EXECUTES. Recording a verdict on a draft must reach the
 *      assessment transition endpoint and nothing else — no `sage_actions`
 *      confirmation, no raw apiFetch to a card route. "I agree with this
 *      reasoning" must never become a blanket authorisation for everything the
 *      reasoning mentions.
 *   2. IT IS ALWAYS LABELLED AI-DRAFTED. In every state, including the ones a
 *      future edit is most likely to touch in isolation (unavailable, approved,
 *      declined). A draft that reads as a colleague's note is the single most
 *      dangerous thing this surface could do.
 *   3. NO SPINNER THAT LIES. `generating` says the alert is already complete;
 *      `unavailable` says WHY, in the server's words; no state renders an
 *      indefinite wait with nothing behind it.
 *   4. DECLINING IS AS CHEAP AS APPROVING, AND RECORDS A REASON. The declined
 *      draft is the most valuable row in the pilot dataset. If declining costs
 *      more taps, or is a smaller/quieter control, the log is biased and so is
 *      every number computed from it.
 *   5. NO EVIDENCE MUST NOT READ AS GUIDELINE-BACKED.
 */
import { render, screen, waitFor, fireEvent, act, within } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import type { SageAssessment } from '../../api/sageAssessment'
import { formatUTCTime } from '../../lib/datetime'

/* vi.hoisted, because vi.mock factories are lifted above every `const` in the
   file and would otherwise read these in their temporal dead zone. */
const mocks = vi.hoisted(() => ({
  getLatestAssessmentForPatient: vi.fn(),
  listSageAssessments: vi.fn(),
  transitionAssessment: vi.fn(),
  /* Mocked so the two paths a regression would most plausibly take to
     EXECUTION are observable: the card-transition helper, and a raw apiFetch.
     Neither is imported by the component under test, which is exactly the
     property being pinned — if somebody wires "approve also confirms the card",
     one of these fires and the test fails. */
  transitionSageAction: vi.fn(),
  apiFetch: vi.fn(),
}))
const {
  getLatestAssessmentForPatient, listSageAssessments, transitionAssessment,
  transitionSageAction, apiFetch,
} = mocks

vi.mock('../../api/sageAssessment', () => ({
  getLatestAssessmentForPatient: mocks.getLatestAssessmentForPatient,
  listSageAssessments: mocks.listSageAssessments,
  transitionAssessment: mocks.transitionAssessment,
}))

vi.mock('../../api/client', () => ({
  apiFetch: mocks.apiFetch,
  transitionSageAction: mocks.transitionSageAction,
  getChannels: vi.fn(async () => []),
  // roles.ts reads this; every test passes an explicit role, so it is only a
  // safety net against an accidental storage read.
  getUser: () => ({ id: 'DOC1', name: 'Dr Iyer', role: 'doctor' }),
  socket: { on: vi.fn(), off: vi.fn(), emit: vi.fn() },
}))

import SageAlertAssessment from './SageAlertAssessment'
import {
  partitionAssessments, patientSpecificView, isGenericAdviceLine,
} from './sageAssessmentView'
import { socket } from '../../api/client'

/** The mocked case-room socket. The component re-reads on
 *  `sage:assessment_update`, and that is the only way to drive a real
 *  silent → flagged transition through the component's own load path rather
 *  than by re-rendering it with different props. */
const socketOn = (socket as unknown as { on: ReturnType<typeof vi.fn> }).on

const CHANNEL = 'ch-bed7'

/** A READY draft, shaped exactly like `sage_assessment_service.render()`. */
function readyAssessment(over: Partial<SageAssessment> = {}): SageAssessment {
  return {
    id: 'asm-1',
    patient_id: 'P007',
    channel_id: CHANNEL,
    message_id: 4021,
    lane: 'ward',
    state: 'ready',
    version: 2,
    unavailable_reason: null,
    unavailable_message: null,
    fallback_text: null,
    news2: 19,
    news2_level: 'critical',
    news2_drivers: ['Respiratory rate 32', 'SpO2 88% on air'],
    whats_happening:
      'Rapid respiratory deterioration over the last two hours with a rising oxygen requirement.',
    considerations: [
      {
        key: 'c0',
        // NOT "Escalate to the on-call registrar now", which this fixture used
        // to carry. That sentence is true of every patient with a NEWS2 19 and
        // is banned on this surface since the 2026-08-15 ruling — a fixture
        // asserting it renders would be a test pinning the behaviour the ruling
        // removed. The generic strings are exercised deliberately, in the
        // filter's own section below.
        step: 'Hold the next metoprolol dose until reviewed',
        why: 'Rate control is masking the tachycardia this score would otherwise show.',
        proposed_action_type: null,
        proposed_target: null,
        orb_can_act: false,
        action_id: null,
        action_type: null,
        advice_only_reason: null,
      },
      {
        key: 'c1',
        step: 'Start IV piperacillin-tazobactam 4.5 g',
        why: 'Suspected hospital-acquired pneumonia.',
        proposed_action_type: 'start_medication',
        proposed_target: 'IV piperacillin-tazobactam 4.5 g',
        orb_can_act: true,
        action_id: 'act-99',
        action_type: 'start_medication',
        advice_only_reason: null,
      },
    ],
    evidence: [
      {
        document_id: 'DOC-7',
        title: 'Sepsis Six — hospital policy',
        source: 'Policy manual',
        trust_tier: 1,
        attribution: 'model_cited',
      },
    ],
    citation_basis: 'model_cited',
    evidence_note: null,
    watch_items: ['Repeat observations in 15 minutes', 'Lactate on the next gas'],
    confidence: 'medium',
    model: 'medgemma:27b',
    generated_ms: 46_300,
    ttft_ms: 7_400,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    disclaimer:
      'Draft decision support. Nothing here has been ordered, charted or administered. '
      + 'Every action requires a clinician\'s explicit approval.',
    ...over,
  }
}

/** Both read paths, kept consistent on purpose.
 *
 *  The component pays for the case-room lookup once and then re-reads through
 *  the cheap per-channel list, so a fixture that stubs only the first one
 *  quietly blanks the card on the second read — which is how the 409 test below
 *  first "passed" against an empty document. */
function serve(assessment: SageAssessment | null, channelId: string | null = CHANNEL) {
  const rows = assessment ? [assessment] : []
  getLatestAssessmentForPatient.mockResolvedValue({ channelId, assessment, rows })
  listSageAssessments.mockResolvedValue(rows)
}

/** A case room holding SEVERAL drafts — one row per critical alert, newest
 *  first, exactly as the endpoint orders them. This is the ordinary state of a
 *  patient who has deteriorated twice, and it is the shape the ward defect was
 *  hiding in. */
function serveRows(rows: SageAssessment[], channelId: string | null = CHANNEL) {
  getLatestAssessmentForPatient.mockResolvedValue({
    channelId, assessment: rows.length > 0 ? rows[0] : null, rows,
  })
  listSageAssessments.mockResolvedValue(rows)
}

async function renderCard(props: Partial<{ riskLevel: string; role: string }> = {}) {
  const view = render(
    <SageAlertAssessment
      patientId="P007"
      riskLevel={props.riskLevel ?? 'critical'}
      role={props.role ?? 'doctor'}
    />,
  )
  // Let the mount read settle so no assertion races the first fetch.
  await act(async () => { await Promise.resolve() })
  return view
}

beforeEach(() => {
  vi.clearAllMocks()
  listSageAssessments.mockResolvedValue([])
  transitionSageAction.mockResolvedValue({ status: 'ok' })
  apiFetch.mockResolvedValue({})
})

/* ══════════════════════════════════════════════════════════════════════════
   1. THE ALERT NEVER WAITS, AND SILENCE IS NEVER FAKED
   ══════════════════════════════════════════════════════════════════════════ */
describe('no draft', () => {
  it('renders nothing at all when the patient has no drafted assessment', async () => {
    serve(null)
    const { container } = await renderCard()
    await waitFor(() => expect(getLatestAssessmentForPatient).toHaveBeenCalled())
    // Not a placeholder, not a spinner, not an empty box: the alert stands
    // alone, which is the correct outcome for every patient who is not the
    // subject of a critical Vigil alert.
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing when the patient has no case room', async () => {
    serve(null, null)
    const { container } = await renderCard()
    await waitFor(() => expect(getLatestAssessmentForPatient).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })

  it('reports a failed read as a failure, never as "no assessment"', async () => {
    getLatestAssessmentForPatient.mockRejectedValue(new Error('network'))
    await renderCard({ riskLevel: 'critical' })
    await screen.findByText(/could not check for an assessment/i)
    // The words that matter: a blank region here would read as an all-clear.
    expect(screen.getByText(/display failure, not an all-clear/i)).toBeInTheDocument()
  })

  it('stays silent on a failed read for a patient with no critical alert', async () => {
    getLatestAssessmentForPatient.mockRejectedValue(new Error('network'))
    const { container } = await renderCard({ riskLevel: 'stable' })
    await waitFor(() => expect(getLatestAssessmentForPatient).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   2. THE AI-DRAFTED LABEL — EVERY STATE, NO EXCEPTIONS
   ══════════════════════════════════════════════════════════════════════════ */
describe('the AI-drafted label', () => {
  const states: Array<[string, SageAssessment]> = [
    ['generating', readyAssessment({ state: 'generating', whats_happening: null })],
    ['ready', readyAssessment()],
    ['unavailable', readyAssessment({
      state: 'unavailable',
      unavailable_reason: 'shed',
      unavailable_message: 'Sage set this aside under load. No assessment was written.',
      fallback_text: 'Ensure continuous monitoring and prepare for possible intervention.',
    })],
    ['approved', readyAssessment({ state: 'approved' })],
    ['declined', readyAssessment({ state: 'declined' })],
  ]

  it.each(states)('is present in the %s state', async (_state, assessment) => {
    serve(assessment)
    await renderCard()
    expect(await screen.findByText('AI DRAFT')).toBeInTheDocument()
    // The tag alone is a badge somebody could read past; the sentence says it
    // in words, and says which side of the line this sits on.
    expect(screen.getByText(/not by a\s+clinician/i)).toBeInTheDocument()
    expect(screen.getByText(/Decision support only/i)).toBeInTheDocument()
  })

  it('never presents the draft as something a clinician wrote', async () => {
    serve(readyAssessment())
    const { container } = await renderCard()
    await screen.findByText('AI DRAFT')
    expect(container.textContent).toMatch(/Written by Orb's clinical model/)
  })

  it('stamps WHEN it was drafted — a snapshot must not read as current advice', async () => {
    serve(readyAssessment({ created_at: '2026-08-14T09:12:00Z' }))
    const { container } = await renderCard()
    await screen.findByText('AI DRAFT')
    expect(container.textContent).toMatch(/Drafted .+ on .+\./)
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   3. EVERY STATE RENDERS, AND SAYS THE TRUE THING
   ══════════════════════════════════════════════════════════════════════════ */
describe('states', () => {
  it('generating says the alert is already complete and does not wait on this', async () => {
    serve(readyAssessment({ state: 'generating', whats_happening: null }))
    await renderCard()
    await screen.findByText(/Sage is writing an assessment/i)
    expect(
      screen.getByText(/alert above is already complete and does not depend on this/i),
    ).toBeInTheDocument()
    // No draft content is invented while it is being written.
    expect(screen.queryByText(/What Sage would consider/i)).not.toBeInTheDocument()
  })

  it('generating never spins forever — a long-stuck draft is reported as no assessment', async () => {
    serve(readyAssessment({
      state: 'generating',
      whats_happening: null,
      // Older than the client's stall ceiling, which itself sits above the
      // server's stale window. Reaching this means our polling stopped, not
      // that the model is slow.
      created_at: new Date(Date.now() - 20 * 60 * 1000).toISOString(),
    }))
    await renderCard()
    await screen.findByText(/No assessment has arrived/i)
    expect(screen.getByText(/do not wait for one/i)).toBeInTheDocument()
    expect(screen.queryByText(/Sage is writing an assessment/i)).not.toBeInTheDocument()
  })

  it('unavailable renders the SERVER\'s reason verbatim, never a client-invented one', async () => {
    const message = 'The careful clinical model is not loaded, and Sage will not write an '
      + 'assessment on the small model. No assessment was written.'
    serve(readyAssessment({
      state: 'unavailable',
      unavailable_reason: 'model_evicted',
      unavailable_message: message,
      whats_happening: null,
      fallback_text: 'Ensure continuous monitoring and prepare for possible intervention.',
    }))
    await renderCard()
    expect(await screen.findByText(message)).toBeInTheDocument()
    // The heading says it too, so the fact survives even if the reason wording
    // is ever missing from the payload.
    expect(screen.getAllByText(/No assessment was written/i).length).toBeGreaterThan(0)
    // The static degraded-mode line is shown but can never pass as reasoning
    // about this patient.
    expect(
      screen.getByText(/same sentence for every alert, not an assessment of this patient/i),
    ).toBeInTheDocument()
  })

  it('unavailable offers no verdict — there is nothing to agree or disagree with', async () => {
    serve(readyAssessment({
      state: 'unavailable',
      unavailable_reason: 'model_error',
      unavailable_message: 'Sage hit an error writing this assessment. No assessment was written.',
    }))
    await renderCard()
    await screen.findAllByText(/No assessment was written/i)
    expect(screen.queryByRole('button', { name: /approve draft/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /decline draft/i })).not.toBeInTheDocument()
  })

  it('ready renders all four sections, interpretation first', async () => {
    serve(readyAssessment())
    const { container } = await renderCard()
    await screen.findByText(/Rapid respiratory deterioration/i)
    expect(screen.getByText(/What Sage would consider/i)).toBeInTheDocument()
    expect(screen.getByText(/Hold the next metoprolol dose until reviewed/i)).toBeInTheDocument()
    expect(screen.getByText(/Watch for/i)).toBeInTheDocument()
    expect(screen.getByText(/Repeat observations in 15 minutes/i)).toBeInTheDocument()
    // The interpretation precedes the considerations in the DOM — this is the
    // ten-seconds-at-a-bedside ordering, and it is a real assertion because
    // reordering the JSX would flip it.
    const text = container.textContent || ''
    expect(text.indexOf('Rapid respiratory deterioration'))
      .toBeLessThan(text.indexOf('Hold the next metoprolol dose'))
  })

  it('approved and declined both keep the draft readable and say nothing executed', async () => {
    for (const state of ['approved', 'declined'] as const) {
      serve(readyAssessment({ state }))
      const { unmount } = await renderCard()
      await screen.findByText(new RegExp(`clinician ${state} this draft`, 'i'))
      expect(screen.getByText(/Rapid respiratory deterioration/i)).toBeInTheDocument()
      expect(
        screen.getAllByText(/Nothing here has been ordered, charted or administered/i).length,
      ).toBeGreaterThan(0)
      unmount()
    }
  })

  it('shows Orb\'s NEWS2 as Orb\'s own, not as the model\'s', async () => {
    serve(readyAssessment())
    await renderCard()
    expect(await screen.findByText('NEWS2 19')).toBeInTheDocument()
    expect(screen.getByText(/does not\s+recalculate it/i)).toBeInTheDocument()
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   4. EVIDENCE — ABSENCE MUST NOT LOOK LIKE PRESENCE
   ══════════════════════════════════════════════════════════════════════════ */
describe('evidence', () => {
  it('keeps a real source list one tap away rather than in the way', async () => {
    serve(readyAssessment())
    await renderCard()
    const toggle = await screen.findByRole('button', { name: /show evidence \(1\)/i })
    expect(screen.queryByText(/Sepsis Six/i)).not.toBeInTheDocument()
    fireEvent.click(toggle)
    expect(screen.getByText('Sepsis Six — hospital policy')).toBeInTheDocument()
    // The TIER is shown, never averaged away: "our SOP says" and "a guideline
    // somewhere says" are weighed very differently at a bedside.
    expect(screen.getByText(/^Hospital policy · Cited by Sage$/)).toBeInTheDocument()
  })

  it('an assessment with no retrieved evidence does not look guideline-backed', async () => {
    const note = 'No guideline in Orb\'s corpus matched this alert. This assessment is model '
      + 'reasoning over the chart alone.'
    serve(readyAssessment({ evidence: [], citation_basis: 'none', evidence_note: note }))
    await renderCard()
    // On screen without being asked for — NOT behind a control labelled
    // "evidence", which would itself imply there is some.
    expect(await screen.findByText(note)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /evidence/i })).not.toBeInTheDocument()
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   5. THE VERDICT — AND THE LINE IT MUST NEVER CROSS
   ══════════════════════════════════════════════════════════════════════════ */
describe('approve / decline', () => {
  it('offers exactly two verdicts, in identical controls', async () => {
    serve(readyAssessment())
    await renderCard()
    const approve = await screen.findByRole('button', { name: /approve draft/i })
    const decline = screen.getByRole('button', { name: /decline draft/i })
    // SAME CLASS = same size, same weight, same colour. A green Approve beside
    // a quiet Decline biases the pilot dataset before a patient is enrolled.
    expect(approve.className).toBe(decline.className)
  })

  it('approving records the verdict and EXECUTES NOTHING', async () => {
    serve(readyAssessment())
    transitionAssessment.mockResolvedValue({
      status: 'ok', assessment: readyAssessment({ state: 'approved', version: 3 }),
    })
    await renderCard()

    fireEvent.click(await screen.findByRole('button', { name: /approve draft/i }))
    fireEvent.click(screen.getByRole('button', { name: /matches my assessment/i }))

    await waitFor(() => expect(transitionAssessment).toHaveBeenCalledTimes(1))
    expect(transitionAssessment).toHaveBeenCalledWith(
      CHANNEL, 'asm-1', 'approved', 2, 'Matches my assessment',
    )

    // ── THE LINE. Approving an assessment is not authorisation for anything it
    // proposes. Every proposed action stays a separate `sage_actions` card with
    // its own confirmation, its own confirm lock and its own drug-safety gate.
    expect(transitionSageAction).not.toHaveBeenCalled()
    const touched = apiFetch.mock.calls.map(c => String(c[0]))
    expect(touched.filter(p => p.includes('sage_actions'))).toEqual([])
    expect(touched.filter(p => p.includes('confirm'))).toEqual([])
  })

  it('shows the proposed action as a card to confirm elsewhere, never as a button here', async () => {
    serve(readyAssessment())
    await renderCard()
    await screen.findByText(/Start IV piperacillin-tazobactam 4\.5 g/i)
    expect(
      screen.getByText(/put this in the patient's case room as a separate card/i),
    ).toBeInTheDocument()
    // No confirm/give/order control anywhere on this surface.
    const labels = screen.getAllByRole('button').map(b => (b.textContent || '').toLowerCase())
    expect(labels.some(l => /confirm|give|administer|order now|start now/.test(l))).toBe(false)
  })

  it('marks a step Orb cannot carry out as advice only', async () => {
    serve(readyAssessment())
    await renderCard()
    await screen.findByText(/Hold the next metoprolol dose until reviewed/i)
    expect(screen.getByText(/Advice only: Orb cannot carry this out/i)).toBeInTheDocument()
  })

  it('declining requires a reason and records it', async () => {
    serve(readyAssessment())
    transitionAssessment.mockResolvedValue({
      status: 'ok', assessment: readyAssessment({ state: 'declined', version: 3 }),
    })
    await renderCard()

    fireEvent.click(await screen.findByRole('button', { name: /decline draft/i }))
    // No way to decline without saying why: the verdict is not submitted by the
    // first press, and there is no bare "confirm" that skips the reason.
    expect(transitionAssessment).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: /i disagree clinically/i }))
    await waitFor(() => expect(transitionAssessment).toHaveBeenCalledTimes(1))
    expect(transitionAssessment).toHaveBeenCalledWith(
      CHANNEL, 'asm-1', 'declined', 2, 'I disagree clinically',
    )
  })

  it('costs declining exactly what approving costs — same taps, same shape', async () => {
    // THE ANTI-BIAS PROPERTY, measured rather than asserted by eye. If declining
    // grows a step (a modal, a confirm, a mandatory free-text box) that
    // approving does not have, this fails.
    const taps: Record<string, number> = {}
    for (const [verdict, chip] of [
      ['approve', /matches my assessment/i],
      ['decline', /i disagree clinically/i],
    ] as const) {
      serve(readyAssessment())
      transitionAssessment.mockResolvedValue({
        status: 'ok', assessment: readyAssessment({ state: 'approved', version: 3 }),
      })
      transitionAssessment.mockClear()
      const { unmount } = await renderCard()
      let n = 0
      fireEvent.click(await screen.findByRole('button', { name: new RegExp(`${verdict} draft`, 'i') }))
      n += 1
      fireEvent.click(screen.getByRole('button', { name: chip }))
      n += 1
      await waitFor(() => expect(transitionAssessment).toHaveBeenCalledTimes(1))
      taps[verdict] = n
      unmount()
    }
    expect(taps.decline).toBe(taps.approve)
  })

  it('offers free text on both verdicts, and will not record an empty one', async () => {
    serve(readyAssessment())
    await renderCard()
    fireEvent.click(await screen.findByRole('button', { name: /decline draft/i }))
    fireEvent.click(screen.getByRole('button', { name: /other/i }))
    const record = screen.getByRole('button', { name: /^record$/i })
    expect(record).toBeDisabled()
    fireEvent.change(screen.getByLabelText(/in your words/i), {
      target: { value: 'Patient already for palliation — this is not the plan.' },
    })
    expect(record).not.toBeDisabled()
    transitionAssessment.mockResolvedValue({
      status: 'ok', assessment: readyAssessment({ state: 'declined', version: 3 }),
    })
    fireEvent.click(record)
    await waitFor(() => expect(transitionAssessment).toHaveBeenCalledWith(
      CHANNEL, 'asm-1', 'declined', 2,
      'Patient already for palliation — this is not the plan.',
    ))
  })

  it('sends the version it read, so a concurrent verdict cannot be overwritten', async () => {
    serve(readyAssessment({ version: 9 }))
    transitionAssessment.mockRejectedValue(
      Object.assign(new Error('Assessment was modified concurrently. Please refresh.'),
        { status: 409 }),
    )
    await renderCard()
    fireEvent.click(await screen.findByRole('button', { name: /approve draft/i }))
    fireEvent.click(screen.getByRole('button', { name: /matches my assessment/i }))
    await waitFor(() => expect(transitionAssessment).toHaveBeenCalledWith(
      CHANNEL, 'asm-1', 'approved', 9, 'Matches my assessment',
    ))
    // The clinician is told, and the surface re-reads rather than retrying.
    expect(await screen.findByText(/modified concurrently/i)).toBeInTheDocument()
  })

  it('offers no verdict to a role the server would refuse, and says who records it', async () => {
    serve(readyAssessment())
    await renderCard({ role: 'nurse' })
    await screen.findByText(/Rapid respiratory deterioration/i)
    expect(screen.queryByRole('button', { name: /approve draft/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /decline draft/i })).not.toBeInTheDocument()
    expect(screen.getByText(/A doctor records the verdict on this draft/i)).toBeInTheDocument()
    // Still fully readable — a nurse holds relay.read and reads every word.
    expect(screen.getByText(/Hold the next metoprolol dose until reviewed/i)).toBeInTheDocument()
  })

  it('states plainly, next to the buttons, that nothing has been ordered', async () => {
    serve(readyAssessment())
    await renderCard()
    await screen.findByRole('button', { name: /approve draft/i })
    expect(
      screen.getByText(/Nothing here has been ordered, charted or administered/i),
    ).toBeInTheDocument()
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   6. A FINISHED ASSESSMENT IS NEVER BURIED BY A LATER FAILED ONE

   THE LIVE DEFECT, from the ward. Ananya Kapoor had a draft at 16:25:12 that
   was `ready` with full content. A second critical alert at 16:26:53 wrote a
   second row into the SAME case room, and that one failed with
   `queue_timeout`. The chart then said "No assessment was written" — 101
   seconds after Sage had successfully written one — because the surface took
   the newest row and nothing else. The finished assessment was in the
   database, unresolved, and unreachable: its approve/decline controls belonged
   to a row nothing would render, which is why Decline could not be reached in
   55 minutes of trying.

   The rule these tests pin: the newest row still governs the CURRENT state
   (the latest alert genuinely has no assessment), and an older row still
   awaiting a verdict survives beside it, labelled with the alert it came from
   and fully reviewable.
   ══════════════════════════════════════════════════════════════════════════ */
describe('an older unresolved draft behind a newer failure', () => {
  /** 16:25:12 — written, complete, nobody has ruled on it. */
  const READY_1625 = readyAssessment({
    id: 'asm-1625',
    message_id: 4021,
    version: 2,
    state: 'ready',
    created_at: '2026-08-14T16:25:12Z',
  })
  /** 16:26:53 — the newer alert, whose draft never arrived. */
  const FAILED_1626 = readyAssessment({
    id: 'asm-1626',
    message_id: 4033,
    version: 1,
    state: 'unavailable',
    unavailable_reason: 'queue_timeout',
    unavailable_message:
      'Sage was still busy with another patient when this alert timed out. '
      + 'No assessment was written.',
    whats_happening: null,
    considerations: [],
    watch_items: [],
    evidence: [],
    evidence_note: null,
    confidence: null,
    created_at: '2026-08-14T16:26:53Z',
  })

  /** The carried block, addressed the way a screen reader reaches it. */
  function carriedRegion() {
    return screen.getByRole('region', { name: /still awaiting your review/i })
  }

  it('still exposes the ready draft, and its approve AND decline controls', async () => {
    serveRows([FAILED_1626, READY_1625])
    await renderCard()
    await screen.findAllByText(/No assessment was written/i)

    // The complete assessment is on screen — the words a clinician would have
    // acted on, not a stub or a link to somewhere else.
    const carried = carriedRegion()
    expect(within(carried).getByText(/Rapid respiratory deterioration/i)).toBeInTheDocument()
    expect(within(carried).getByText(/Hold the next metoprolol dose until reviewed/i)).toBeInTheDocument()

    // And it is RESOLVABLE from here. This is the affordance that could not be
    // reached on the ward: both verdicts, in the same control, on the draft
    // they belong to.
    const approve = within(carried).getByRole('button', { name: /approve draft/i })
    const decline = within(carried).getByRole('button', { name: /decline draft/i })
    expect(approve).toBeInTheDocument()
    expect(decline).toBeInTheDocument()
    // Same class, so declining a carried draft costs exactly what approving it
    // costs — the anti-bias property, held on this copy of the controls too.
    expect(approve.className).toBe(decline.className)
  })

  it('says WHICH alert the surfaced draft belongs to', async () => {
    serveRows([FAILED_1626, READY_1625])
    await renderCard()
    await screen.findAllByText(/No assessment was written/i)
    // The older draft's own time, not the newer alert's — an assessment shown
    // under the wrong alert is worse than one that is missing.
    const older = formatUTCTime('2026-08-14T16:25:12Z')
    const newer = formatUTCTime('2026-08-14T16:26:53Z')
    expect(older).not.toBe(newer)
    expect(
      within(carriedRegion()).getByText(new RegExp(`assessment from the ${older} alert`, 'i')),
    ).toBeInTheDocument()
  })

  it('keeps the CURRENT state honest — the newest alert has no assessment', async () => {
    serveRows([FAILED_1626, READY_1625])
    const { container } = await renderCard()
    await screen.findAllByText(/No assessment was written/i)

    // The server's wording for the newest row, verbatim, and its state chip.
    expect(screen.getByText(FAILED_1626.unavailable_message as string)).toBeInTheDocument()
    expect(screen.getByText('No assessment')).toBeInTheDocument()
    // Reading order: the current alert's state first, the carried draft after.
    // Surfacing an older draft must not demote the alert being looked at.
    const text = container.textContent || ''
    expect(text.indexOf('No assessment was written'))
      .toBeLessThan(text.indexOf('still awaiting your review'))
  })

  it('labels the carried draft AI-drafted too — every state, every copy', async () => {
    serveRows([FAILED_1626, READY_1625])
    await renderCard()
    await screen.findAllByText(/No assessment was written/i)
    const carried = within(carriedRegion())
    // The tag AND the sentence. A draft resurfaced from an earlier alert is the
    // most likely thing on this screen to be mistaken for a colleague's note.
    expect(carried.getByText('AI DRAFT')).toBeInTheDocument()
    expect(carried.getByText(/Written by Orb's clinical model/i)).toBeInTheDocument()
    expect(carried.getByText(/not\s+by a clinician/i)).toBeInTheDocument()
    expect(carried.getByText(/Decision support only/i)).toBeInTheDocument()
    expect(carried.getByText(/Nobody has approved or declined it/i)).toBeInTheDocument()
  })

  it('records the verdict against the OLDER row, not the newest one', async () => {
    serveRows([FAILED_1626, READY_1625])
    transitionAssessment.mockResolvedValue({
      status: 'ok',
      assessment: { ...READY_1625, state: 'approved', version: 3 },
    })
    await renderCard()
    await screen.findAllByText(/No assessment was written/i)

    const carried = within(carriedRegion())
    fireEvent.click(carried.getByRole('button', { name: /decline draft/i }))
    fireEvent.click(carried.getByRole('button', { name: /i disagree clinically/i }))

    await waitFor(() => expect(transitionAssessment).toHaveBeenCalledTimes(1))
    // asm-1625 at ITS version — resolving the failed newest row instead would
    // leave the real draft unresolved and mark the wrong one reviewed.
    expect(transitionAssessment).toHaveBeenCalledWith(
      CHANNEL, 'asm-1625', 'declined', 2, 'I disagree clinically',
    )
    // Still nothing executed from this copy of the controls either.
    expect(transitionSageAction).not.toHaveBeenCalled()
    const touched = apiFetch.mock.calls.map(c => String(c[0]))
    expect(touched.filter(p => p.includes('sage_actions'))).toEqual([])
  })

  it('does not surface a draft that is already resolved, or one still writing', async () => {
    // Only an UNRESOLVED older draft is carried. An approved one has had its
    // verdict, and a `generating` one is not a finished assessment at all — a
    // surface that resurfaced either would be crying wolf.
    serveRows([
      FAILED_1626,
      readyAssessment({ id: 'asm-old-approved', state: 'approved' }),
      readyAssessment({ id: 'asm-old-generating', state: 'generating', whats_happening: null }),
      readyAssessment({ id: 'asm-old-declined', state: 'declined' }),
    ])
    await renderCard()
    await screen.findAllByText(/No assessment was written/i)
    expect(screen.queryByRole('region', { name: /still awaiting your review/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /approve draft/i })).toBeNull()
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   7. SEVERAL UNRESOLVED DRAFTS ARE NOT A WALL

   The fix for "one draft was hidden" must not be "five drafts are stacked
   under a critical alert". One is shown in full and reviewable; the rest are
   counted in a sentence and arrive one at a time.
   ══════════════════════════════════════════════════════════════════════════ */
describe('several unresolved drafts', () => {
  const NEWEST_FAILED = readyAssessment({
    id: 'asm-newest',
    state: 'unavailable',
    unavailable_reason: 'queue_timeout',
    unavailable_message: 'Sage was still busy when this alert timed out. No assessment was written.',
    whats_happening: null,
    considerations: [],
    watch_items: [],
    evidence: [],
    created_at: '2026-08-14T16:26:53Z',
  })
  const UNRESOLVED = [
    readyAssessment({
      id: 'asm-a', version: 2, whats_happening: 'FIRST unresolved draft.',
      created_at: '2026-08-14T16:25:12Z',
    }),
    readyAssessment({
      id: 'asm-b', version: 4, whats_happening: 'SECOND unresolved draft.',
      created_at: '2026-08-14T15:02:00Z',
    }),
    readyAssessment({
      id: 'asm-c', version: 6, whats_happening: 'THIRD unresolved draft.',
      created_at: '2026-08-14T11:40:00Z',
    }),
  ]

  it('shows one in full and COUNTS the rest', async () => {
    serveRows([NEWEST_FAILED, ...UNRESOLVED])
    await renderCard()
    await screen.findAllByText(/No assessment was written/i)

    // Exactly one carried draft is rendered — not three.
    expect(screen.getAllByRole('region', { name: /still awaiting your review/i })).toHaveLength(1)
    expect(screen.getByText('FIRST unresolved draft.')).toBeInTheDocument()
    expect(screen.queryByText('SECOND unresolved draft.')).toBeNull()
    expect(screen.queryByText('THIRD unresolved draft.')).toBeNull()
    // One verdict to make, not three — and the others are stated, not hidden.
    expect(screen.getAllByRole('button', { name: /approve draft/i })).toHaveLength(1)
    expect(screen.getByText(/2 further assessments/i)).toBeInTheDocument()
  })

  it('counts in the singular when exactly one other is waiting', async () => {
    serveRows([NEWEST_FAILED, UNRESOLVED[0], UNRESOLVED[1]])
    await renderCard()
    await screen.findAllByText(/No assessment was written/i)
    expect(screen.getByText(/1 further assessment for this patient is also awaiting/i))
      .toBeInTheDocument()
  })

  it('says nothing about others when the surfaced one is the only one', async () => {
    serveRows([NEWEST_FAILED, UNRESOLVED[0]])
    await renderCard()
    await screen.findAllByText(/No assessment was written/i)
    expect(screen.queryByText(/further assessment/i)).toBeNull()
  })

  it('confirms a recorded verdict and brings the next one up', async () => {
    serveRows([NEWEST_FAILED, ...UNRESOLVED])
    transitionAssessment.mockResolvedValue({
      status: 'ok', assessment: { ...UNRESOLVED[0], state: 'approved', version: 3 },
    })
    await renderCard()
    await screen.findAllByText(/No assessment was written/i)

    const carried = within(screen.getByRole('region', { name: /still awaiting your review/i }))
    fireEvent.click(carried.getByRole('button', { name: /approve draft/i }))
    fireEvent.click(carried.getByRole('button', { name: /matches my assessment/i }))
    await waitFor(() => expect(transitionAssessment).toHaveBeenCalledTimes(1))

    // The verdict is confirmed on screen — a draft that simply vanishes when
    // resolved is the same class of failure as one that vanishes when it fails.
    expect(await screen.findByText(/A clinician approved the assessment from the/i))
      .toBeInTheDocument()
    // And the next unresolved draft takes its place in the same beat, without
    // waiting for a re-read that may be a polling interval away.
    expect(await screen.findByText('SECOND unresolved draft.')).toBeInTheDocument()
    expect(screen.queryByText('FIRST unresolved draft.')).toBeNull()
    expect(screen.getByText(/1 further assessment for this patient is also awaiting/i))
      .toBeInTheDocument()
  })

  it('offers a nurse no verdict on a carried draft, and says who records it', async () => {
    serveRows([NEWEST_FAILED, ...UNRESOLVED])
    await renderCard({ role: 'nurse' })
    await screen.findAllByText(/No assessment was written/i)
    const carried = within(screen.getByRole('region', { name: /still awaiting your review/i }))
    expect(carried.queryByRole('button', { name: /approve draft/i })).toBeNull()
    expect(carried.queryByRole('button', { name: /decline draft/i })).toBeNull()
    // Readable, and honest about why there is nothing to press.
    expect(carried.getByText('FIRST unresolved draft.')).toBeInTheDocument()
    expect(carried.getByText(/A doctor records the verdict on this draft/i)).toBeInTheDocument()
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   8. THE RULE ITSELF, WITHOUT A SCREEN

   `partitionAssessments` is the one place that decides which row wins and
   which rows survive. Tested directly so the rule cannot be quietly changed
   under a passing render.
   ══════════════════════════════════════════════════════════════════════════ */
describe('partitionAssessments', () => {
  const ready = (id: string) => readyAssessment({ id, state: 'ready' })

  it('gives the newest row the current state and keeps an older ready one', () => {
    const failed = readyAssessment({ id: 'new', state: 'unavailable' })
    const { current, carried } = partitionAssessments([failed, ready('old')])
    expect(current?.id).toBe('new')
    expect(carried.map(a => a.id)).toEqual(['old'])
  })

  it('carries nothing that is finished with, or not yet finished', () => {
    const { carried } = partitionAssessments([
      readyAssessment({ id: 'new', state: 'unavailable' }),
      readyAssessment({ id: 'a', state: 'approved' }),
      readyAssessment({ id: 'd', state: 'declined' }),
      readyAssessment({ id: 'g', state: 'generating' }),
      readyAssessment({ id: 'u', state: 'unavailable' }),
    ])
    expect(carried).toEqual([])
  })

  it('never carries the current row, even when it is ready', () => {
    const { current, carried } = partitionAssessments([ready('new'), ready('old')])
    expect(current?.id).toBe('new')
    // The newest ready draft is rendered as the current one; carrying it as
    // well would show the same draft twice with two sets of controls.
    expect(carried.map(a => a.id)).toEqual(['old'])
  })

  it('is empty on an empty room', () => {
    expect(partitionAssessments([])).toEqual({ current: null, carried: [] })
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   9. THE READ SHAPE
   ══════════════════════════════════════════════════════════════════════════ */
describe('the first read', () => {
  it('renders the newest draft even from a payload that carries no row list', async () => {
    // A caller that can only tell us about the newest row degrades to exactly
    // that — the current state renders, and no older draft is invented from it.
    getLatestAssessmentForPatient.mockResolvedValue({
      channelId: CHANNEL, assessment: readyAssessment(),
    })
    await renderCard()
    expect(await screen.findByText(/Rapid respiratory deterioration/i)).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: /still awaiting your review/i })).toBeNull()
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   10. TIER 1 — THE HALF THAT IS NEVER PENDING

   The server computes the driver/trend block BEFORE any generation starts (no
   model, 33-240 ms), stores it, and emits it on every payload whatever `state`
   says. Until 2026-08-15 no component read it: `grep -rn "tier1" src/` returned
   nothing, and a patient whose systolic BP fell 108 → 65 across six
   observations had the sentence describing that fall sitting in the database
   while the chart said "Trend: → Stable".

   The properties here are about STATE, which is what this file can prove and
   the renderer's own suite cannot: Tier 1 is not the model's to lose, so no
   generation failure may retract it; and there is never a second copy of it.
   ══════════════════════════════════════════════════════════════════════════ */
const TIER1_MOVEMENT =
  'Systolic BP 65mmHg, down from 108mmHg across the last 6 observations, NEWS2 points 1 → 3'

function tier1Block(over: Record<string, unknown> = {}) {
  return {
    available: true,
    source: 'orb_deterministic_engine',
    scoring_model: 'NEWS2',
    spo2_scale: 1,
    news2: 12,
    news2_band: 'critical',
    single_param_3: true,
    complete: true,
    missing_parameters: [],
    coverage_note: null,
    observed_at: '2026-08-14T09:32:00Z',
    observations_used: 6,
    window_from: '2026-08-14T04:02:00Z',
    window_to: '2026-08-14T09:32:00Z',
    parameters: [{
      parameter: 'Systolic BP', value: 65, points: 3, note: 'Hypotension (BP 65)',
      display: '65mmHg', unit: 'mmHg', previous_value: 82, previous_points: 2,
      first_value: 108, first_points: 1, value_delta: -43, points_delta: 2,
      points_direction: 'worsening', value_direction: 'down', movement: TIER1_MOVEMENT,
    }],
    non_scoring: [],
    most_abnormal: null,
    most_abnormal_tied_with: [],
    trend: 'worsening',
    news2_delta: 9,
    computed_at: '2026-08-14T09:32:01Z',
    headline: 'NEWS2 12 (critical), up 9 across the last 6 observations.',
    ...over,
  }
}

describe('Tier 1 inside the draft', () => {
  const states: Array<[string, SageAssessment]> = [
    ['generating', readyAssessment({
      state: 'generating', whats_happening: null, considerations: [],
      tier1: tier1Block(), has_tier1: true, has_advice: false,
    })],
    ['ready', readyAssessment({ tier1: tier1Block(), has_tier1: true })],
    ['unavailable', readyAssessment({
      state: 'unavailable',
      unavailable_reason: 'queue_timeout',
      unavailable_message: 'Sage was still waiting for the model. No assessment was written.',
      whats_happening: null, considerations: [],
      tier1: tier1Block(), has_tier1: true, has_advice: false,
    })],
    ['approved', readyAssessment({ state: 'approved', tier1: tier1Block(), has_tier1: true })],
    ['declined', readyAssessment({ state: 'declined', tier1: tier1Block(), has_tier1: true })],
  ]

  it.each(states)('renders in the %s state — no generation failure retracts it', async (
    _state, row,
  ) => {
    serve(row)
    await renderCard()
    expect(await screen.findByText(TIER1_MOVEMENT)).toBeInTheDocument()
    expect(screen.getByText('↘ Worsening')).toBeInTheDocument()
  })

  it('sits ABOVE the pending half — a clinician is never told to wait for what they have',
    async () => {
      serve(readyAssessment({
        state: 'generating', whats_happening: null, considerations: [],
        tier1: tier1Block(), has_tier1: true,
      }))
      const { container } = await renderCard()
      await screen.findByText(TIER1_MOVEMENT)
      const text = container.textContent || ''
      expect(text.indexOf(TIER1_MOVEMENT))
        .toBeLessThan(text.indexOf('Sage is writing an assessment'))
    })

  it('is labelled as Orb\'s own arithmetic, inside a block headed AI DRAFT', async () => {
    serve(readyAssessment({ tier1: tier1Block(), has_tier1: true }))
    await renderCard()
    await screen.findByText(TIER1_MOVEMENT)
    // The section says "AI DRAFT" at the top; this block inside it must not be
    // read as part of what the model wrote.
    expect(screen.getByText(/computed by Orb, not written by a model/i)).toBeInTheDocument()
  })

  it('renders ONCE, even with an older unresolved draft on screen', async () => {
    const now = new Date().toISOString()
    const older = new Date(Date.now() - 90_000).toISOString()
    serveRows([
      readyAssessment({
        id: 'new', state: 'unavailable', unavailable_reason: 'queue_timeout',
        unavailable_message: 'Sage was still waiting for the model.',
        whats_happening: null, considerations: [], created_at: now,
        tier1: tier1Block(), has_tier1: true,
      }),
      readyAssessment({
        id: 'old', state: 'ready', created_at: older, updated_at: older,
        tier1: tier1Block({ news2: 9, headline: 'NEWS2 9 (critical), up 4.' }),
        has_tier1: true,
      }),
    ])
    await renderCard()
    await screen.findByText(/still awaiting\s+your review/i)

    // Two drafts, ONE decomposition. Two snapshots of two different moments,
    // stacked, is a reader working out which score belongs to which observation
    // on a patient who is deteriorating.
    expect(document.querySelectorAll('.news2-drivers')).toHaveLength(1)
    expect(screen.queryByText('NEWS2 9 (critical), up 4.')).not.toBeInTheDocument()
  })

  it('renders no block at all when the payload carries none', async () => {
    serve(readyAssessment())
    await renderCard()
    await screen.findByText('AI DRAFT')
    // Not an empty panel and not a placeholder: an older row written before
    // Tier 1 existed simply has no block, and the chart keeps its own.
    expect(document.querySelectorAll('.news2-drivers')).toHaveLength(0)
  })

  it('reports to the chart whether it is showing one', async () => {
    const shown = vi.fn()
    serve(readyAssessment({ tier1: tier1Block(), has_tier1: true }))
    render(
      <SageAlertAssessment patientId="P007" riskLevel="critical" role="doctor"
        onTier1Shown={shown} />,
    )
    await screen.findByText(TIER1_MOVEMENT)
    // The chart stands its own live breakdown down on the strength of this.
    await waitFor(() => expect(shown).toHaveBeenCalledWith(true))
  })

  it('reports FALSE when there is no block, so the chart keeps its own', async () => {
    const shown = vi.fn()
    serve(readyAssessment())
    render(
      <SageAlertAssessment patientId="P007" riskLevel="critical" role="doctor"
        onTier1Shown={shown} />,
    )
    await screen.findByText('AI DRAFT')
    await waitFor(() => expect(shown).toHaveBeenCalledWith(false))
    expect(shown).not.toHaveBeenCalledWith(true)
  })
})

describe('the pinned score line, beside Tier 1', () => {
  it('keeps the score and drops its truncated driver strings', async () => {
    serve(readyAssessment({ tier1: tier1Block(), has_tier1: true }))
    await renderCard()
    await screen.findByText(TIER1_MOVEMENT)

    // The score is Orb's and stays — it is what the model was PINNED to.
    expect(screen.getByText('NEWS2 19')).toBeInTheDocument()
    // The three strings are the same parameters as the block below, capped at
    // three and with the movement removed. One list, not two.
    expect(screen.queryByText('SpO2 88% on air')).not.toBeInTheDocument()
  })

  it('keeps them when there is no Tier 1 block to replace them', async () => {
    serve(readyAssessment())
    await renderCard()
    await screen.findByText('AI DRAFT')
    expect(screen.getByText('SpO2 88% on air')).toBeInTheDocument()
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   11. THE ADVICE TIER — PATIENT-SPECIFIC ONLY, AND USUALLY SILENT

   THE RULING, 2026-08-15. The old advice tier was measured at 22% delivery
   overall and 0% across a quiet control window, and when it did arrive two of
   its three steps restated the alert ("Escalate immediately", "Critical
   NEWS2") on a surface that had already stated the score, the drivers and the
   trend deterministically in 33-240 ms. It was decoration.

   It now says ONLY what depends on this patient's own chart, which means on
   most alerts it says NOTHING — and that is the design working, not failing.

   THE PROPERTIES THIS SECTION PINS, IN ORDER OF HOW BADLY LOSING THEM WOULD
   HURT A PATIENT:

     A. SILENT AND FAILED NEVER COLLAPSE. "Orb checked this chart and there is
        nothing to flag" and "the check never ran" are OPPOSITE facts about a
        deteriorating patient. If the second ever renders as the first, the
        surface has asserted a negative clinical finding Orb never made.
     B. SILENCE IS NOT REASSURANCE. A quiet settled line under a NEWS2 19 must
        never read as "this patient is fine".
     C. SILENCE IS NOT PENDING OR BROKEN. It reads as a clinician's finding.
     D. EVERY FLAG NAMES ITS SOURCE FACT, and the fact is visually separable
        from the judgement so it can be checked in one glance.
     E. NOTHING GENERIC RENDERS, in either channel — the new block or the
        older `considerations` field a half-migrated server can still fill.
     F. NOTHING EXECUTES, and declining stays exactly as cheap as approving,
        through the silent → flagged transition.
   ══════════════════════════════════════════════════════════════════════════ */

/** A flag, shaped as `patient_specific.flags[]`. The fact comes from the chart
 *  and the caution is the judgement about it — the two halves this surface has
 *  to keep visually apart. */
function flag(over: Record<string, unknown> = {}) {
  return {
    key: 'ps0',
    fact: 'on metoprolol 25 mg BD',
    fact_source: 'Medications',
    fact_recorded: 'charted 12 Aug',
    caution: 'Heart rate 78 may be falsely reassuring during this deterioration',
    relates_to: 'Heart rate 78',
    ...over,
  }
}

/** THE COMMON CASE: the deterministic pre-screen found nothing on this chart,
 *  so the model was never called at all. Instant, free, and correct. */
const SILENT = {
  outcome: 'no_candidates' as const,
  flags: [],
  model_called: false,
  candidates_n: 0,
  dropped_generic: 0,
  dropped_unsourced: 0,
  screen_ms: 4,
  message: null,
}

const SILENT_LINE = 'No patient-specific factors identified.'
const UNCHECKED_LINE = /Treat this alert as/i

/** The block, addressed the way a screen reader reaches it. */
function adviceBlock(): HTMLElement {
  const el = document.querySelector('.sage-ps')
  if (!el) throw new Error('no advice-tier block rendered')
  return el as HTMLElement
}

describe('the advice tier: silence as a finding', () => {
  it('renders "no patient-specific factors identified" as a settled statement', async () => {
    serve(readyAssessment({ patient_specific: SILENT }))
    await renderCard()
    expect(await screen.findByText(SILENT_LINE)).toBeInTheDocument()
    // It says WHAT WAS CHECKED. A bare "nothing found" is indistinguishable
    // from a feature that is switched off; naming the chart it read is what
    // turns it into a finding.
    expect(screen.getByText(/checked this patient's medications, allergies/i)).toBeInTheDocument()
    expect(screen.getByText(/This is a finding, not a gap/i)).toBeInTheDocument()
  })

  it('never reads as pending, loading or broken', async () => {
    serve(readyAssessment({ patient_specific: SILENT }))
    const { container } = await renderCard()
    await screen.findByText(SILENT_LINE)
    const block = adviceBlock()
    // No skeleton, no spinner, no progressbar inside the block, and none of the
    // vocabulary of something still on its way.
    expect(block.querySelector('.orb-skeleton')).toBeNull()
    expect(within(block).queryByRole('progressbar')).toBeNull()
    expect(block.textContent || '').not.toMatch(/pending|loading|writing|waiting|unavailable|error/i)
    // And nothing anywhere on the card claims the advice half is still coming.
    expect(container.textContent || '').not.toMatch(/Sage is writing/i)
  })

  it('is not reassurance — it never says anything about how sick the patient is', async () => {
    serve(readyAssessment({ patient_specific: SILENT }))
    await renderCard()
    await screen.findByText(SILENT_LINE)
    // THE SAFETY LINE. A quiet settled sentence sitting under a NEWS2 19 is
    // exactly the shape a clinician can misread as an all-clear.
    expect(screen.getByText(/not a statement that the patient is well/i)).toBeInTheDocument()
    expect(screen.getByText(/what say how sick they are/i)).toBeInTheDocument()
  })

  it('is visually quiet — no status colour, no fill, no warning geometry', async () => {
    serve(readyAssessment({ patient_specific: SILENT }))
    await renderCard()
    await screen.findByText(SILENT_LINE)
    const block = adviceBlock()
    // A tier with no warning to give must not occupy the colour or the space of
    // one. The silent modifier is the flat treatment; the warn left-rule lives
    // on the UNCHECKED modifier and must never be reached from here.
    expect(block.className).toContain('sage-ps--silent')
    expect(block.className).not.toContain('sage-ps--unchecked')
    expect(block.className).not.toContain('sage-ps--flags')
    expect(block.getAttribute('data-state')).toBe('silent')
  })

  it('renders in every state the payload carries it in, including while writing', async () => {
    // `no_candidates` is decided deterministically before any model runs, so
    // there is nothing to wait for. Hiding it behind `ready` would tell a
    // clinician to wait for an answer Orb already has.
    for (const state of ['generating', 'ready', 'approved', 'declined'] as const) {
      serve(readyAssessment({
        state,
        whats_happening: state === 'generating' ? null : 'Rapid respiratory deterioration.',
        patient_specific: SILENT,
      }))
      const { unmount } = await renderCard()
      expect(await screen.findByText(SILENT_LINE)).toBeInTheDocument()
      unmount()
    }
  })

  it('says nothing at all when the payload carries no block', async () => {
    // A row written before this tier existed. An absent block is not a finding,
    // and inventing "nothing to flag" from a schema gap would be the same lie
    // as inventing a flag.
    serve(readyAssessment())
    await renderCard()
    await screen.findByText('AI DRAFT')
    expect(document.querySelector('.sage-ps')).toBeNull()
    expect(screen.queryByText(SILENT_LINE)).toBeNull()
  })
})

/* ── A. THE DISTINCTION THE PILOT TURNS ON ────────────────────────────────── */
describe('the advice tier: silent is never confused with failed', () => {
  it('renders a FAILED check as unchecked, never as "nothing to flag"', async () => {
    const message = 'The careful clinical model was not reachable for the patient-specific check.'
    serve(readyAssessment({
      patient_specific: {
        ...SILENT, outcome: 'failed', model_called: true, candidates_n: 3, message,
      },
    }))
    await renderCard()
    expect(await screen.findByText(/could not complete the patient-specific check/i))
      .toBeInTheDocument()
    // THE PROPERTY. The negative finding is never asserted.
    expect(screen.queryByText(SILENT_LINE)).toBeNull()
    expect(screen.getByText(UNCHECKED_LINE)).toBeInTheDocument()
    expect(screen.getByText(/not as\s+clear/i)).toBeInTheDocument()
    // The server's wording for the failure, verbatim.
    expect(screen.getByText(message)).toBeInTheDocument()
    // And it LOOKS different: this is the one state in this tier allowed a
    // status colour, and the silent state must never reach it.
    expect(adviceBlock().className).toContain('sage-ps--unchecked')
    expect(adviceBlock().getAttribute('data-state')).toBe('unchecked-failed')
  })

  it('treats an outcome it does not understand as unchecked, never as silence', async () => {
    // A server that grows a fifth outcome, or a typo in a migration, must not be
    // able to turn into a negative clinical finding on a ward screen.
    serve(readyAssessment({
      patient_specific: {
        ...SILENT,
        outcome: 'partially_screened' as unknown as 'failed',
      },
    }))
    await renderCard()
    expect(await screen.findByText(/could not complete the patient-specific check/i))
      .toBeInTheDocument()
    expect(screen.queryByText(SILENT_LINE)).toBeNull()
  })

  it('reports lines it withheld itself as unchecked, and says how many and why', async () => {
    // The server said it had flags; every one of them was dropped here. That is
    // neither "nothing to flag" nor a clean server failure, and collapsing it
    // into either would hide a filter firing on a live ward.
    serve(readyAssessment({
      patient_specific: {
        ...SILENT,
        outcome: 'flags',
        model_called: true,
        candidates_n: 2,
        flags: [
          flag({ key: 'g', caution: 'Escalate immediately' }),
          flag({ key: 'n', fact: '   ', caution: 'Renal impairment may matter here' }),
        ],
      },
    }))
    await renderCard()
    expect(await screen.findByText(/patient-specific note was withheld/i)).toBeInTheDocument()
    expect(screen.getByText(/1 line that restated the alert and 1 line that named no chart fact/i))
      .toBeInTheDocument()
    expect(screen.queryByText(SILENT_LINE)).toBeNull()
    expect(screen.queryByText('Escalate immediately')).toBeNull()
    expect(adviceBlock().getAttribute('data-state')).toBe('unchecked-withheld')
  })
})

/* ── D. THE FACT, SEPARABLE FROM THE JUDGEMENT ────────────────────────────── */
describe('the advice tier: a flag is a checkable claim', () => {
  const FLAGGED = {
    ...SILENT,
    outcome: 'flags' as const,
    model_called: true,
    candidates_n: 3,
    flags: [flag()],
  }

  it('renders the chart fact in its own labelled element, apart from the judgement', async () => {
    serve(readyAssessment({ patient_specific: FLAGGED }))
    await renderCard()

    const caution = await screen.findByText(
      'Heart rate 78 may be falsely reassuring during this deterioration',
    )
    const fact = screen.getByText('on metoprolol 25 mg BD')
    // TWO ELEMENTS, NOT ONE STRING. A clinician must be able to verify "on
    // metoprolol 25 mg BD" without reading a word of the reasoning, so the fact
    // cannot be a clause inside the caution's own node.
    expect(fact).not.toBe(caution)
    expect(caution.contains(fact)).toBe(false)
    expect(caution.className).toContain('sage-ps__caution')
    expect(fact.className).toContain('sage-ps__fact-text')
    // And the fact is LABELLED as the chart's, so its provenance is not
    // something the reader has to infer from typography alone.
    expect(screen.getByText(/from this chart/i)).toBeInTheDocument()
    // Where to check it, and when it was recorded.
    expect(screen.getByText(/Medications · charted 12 Aug · Heart rate 78/)).toBeInTheDocument()
  })

  it('says these are cautions and that approving carries nothing out', async () => {
    serve(readyAssessment({ patient_specific: FLAGGED }))
    await renderCard()
    await screen.findByText(/falsely reassuring/i)
    expect(screen.getByText(/Cautions, not orders/i)).toBeInTheDocument()
    expect(screen.getByText(/approving this draft carries nothing out/i)).toBeInTheDocument()
    // NOT A CONTROL. There is nothing to press inside this block at all.
    expect(within(adviceBlock()).queryAllByRole('button')).toHaveLength(0)
  })

  it('drops a caution with no chart fact behind it, and counts it', async () => {
    serve(readyAssessment({
      patient_specific: {
        ...FLAGGED,
        flags: [flag(), flag({ key: 'bare', fact: '', caution: 'Consider renal function' })],
      },
    }))
    await renderCard()
    await screen.findByText(/falsely reassuring/i)
    // "Consider renal function" is exactly the unverifiable form the ruling
    // bans — a confident claim with nothing to check it against.
    expect(screen.queryByText('Consider renal function')).toBeNull()
    expect(screen.getByText(/withheld 1 line that named no chart fact/i)).toBeInTheDocument()
  })

  it('counts one flag in the singular and several in the plural', async () => {
    serve(readyAssessment({ patient_specific: FLAGGED }))
    const one = await renderCard()
    expect(await screen.findByText(/^One thing on this patient's chart changes/)).toBeInTheDocument()
    one.unmount()

    serve(readyAssessment({
      patient_specific: {
        ...FLAGGED,
        flags: [flag(), flag({ key: 'ps1', fact: 'creatinine 180, six days ago', fact_source: 'Results', caution: 'A standard fluid bolus needs review first' })],
      },
    }))
    await renderCard()
    expect(await screen.findByText(/^2 things on this patient's chart change/)).toBeInTheDocument()
    expect(screen.getByText('creatinine 180, six days ago')).toBeInTheDocument()
  })

  it('gives a carried draft its own advice tier, so it can honestly be ruled on', async () => {
    const failedNewest = readyAssessment({
      id: 'asm-new', state: 'unavailable', unavailable_reason: 'queue_timeout',
      unavailable_message: 'Sage was still busy when this alert timed out. No assessment written.',
      whats_happening: null, considerations: [], watch_items: [], evidence: [],
      created_at: '2026-08-15T16:26:53Z',
    })
    const carriedReady = readyAssessment({
      id: 'asm-old', version: 2, created_at: '2026-08-15T16:25:12Z',
      patient_specific: FLAGGED,
    })
    serveRows([failedNewest, carriedReady])
    await renderCard()
    const carried = within(await screen.findByRole('region', { name: /still awaiting your review/i }))
    // A draft reachable only in a cut-down form is one nobody can honestly
    // approve — and approving is exactly what this surface is asking for.
    expect(carried.getByText(/falsely reassuring/i)).toBeInTheDocument()
    expect(carried.getByText('on metoprolol 25 mg BD')).toBeInTheDocument()
  })
})

/* ── E. NEVER RESTATE THE ALERT ───────────────────────────────────────────── */
describe('the advice tier: the generic-line filter', () => {
  /* THE EXACT STRINGS OBSERVED ON SCREEN in the review that produced the
     ruling, plus the neighbours a model reaches for next. Every one of them is
     true of any patient with those numbers, and the block above them has
     already said the score, the drivers and the trend. */
  const OBSERVED = [
    'Escalate immediately',
    'Repeat observations',
    'Assess response to initial interventions',
    'Critical NEWS2',
    'Escalate to the on-call registrar now',
    'Repeat observations in 15 minutes',
    'Monitor the patient closely',
    'Consider sepsis',
    'Review the patient urgently',
    'Ensure IV access',
    'Commence oxygen',
    'Urgent senior review',
  ]

  it.each(OBSERVED)('catches %s', line => {
    expect(isGenericAdviceLine(line)).toBe(true)
  })

  /* THE OTHER HALF OF THE RULE, AND THE HARDER ONE. A filter that also deletes
     the chart-anchored sentences has removed the entire point of the tier, so
     these must all SURVIVE. Each names something only this patient's chart
     could supply. */
  const MUST_SURVIVE = [
    'Escalate early: on prednisolone 30 mg, a normal temperature does not exclude sepsis',
    'Heart rate 78 may be falsely reassuring — on metoprolol 25 mg BD',
    'A standard fluid bolus needs review: creatinine 180 six days ago',
    'Penicillin allergy documented 2024 — the usual first-line agent is out',
    'On apixaban 5 mg BD; a fall or a line here carries added bleeding risk',
    'Repeat the lactate rather than the observations — the last one was 4.2',
  ]

  it.each(MUST_SURVIVE)('leaves the chart-anchored line intact: %s', line => {
    expect(isGenericAdviceLine(line)).toBe(false)
  })

  it('treats empty and missing lines as neither generic nor renderable', () => {
    expect(isGenericAdviceLine('')).toBe(false)
    expect(isGenericAdviceLine(null)).toBe(false)
    expect(isGenericAdviceLine(undefined)).toBe(false)
  })

  it('drops a generic step from the older considerations channel, and says so', async () => {
    // The ban holds wherever a line appears on this surface, not only in the
    // new block. A half-migrated server can still fill `considerations`, and a
    // ruling that holds in one lane and leaks in the other is the guardrail
    // lane-parity failure all over again.
    serve(readyAssessment({
      considerations: [
        {
          key: 'g0', step: 'Escalate immediately', why: 'NEWS2 19.',
          proposed_action_type: null, proposed_target: null, orb_can_act: false,
          action_id: null, action_type: null, advice_only_reason: null,
        },
        {
          key: 'g1', step: 'Assess response to initial interventions', why: null,
          proposed_action_type: null, proposed_target: null, orb_can_act: false,
          action_id: null, action_type: null, advice_only_reason: null,
        },
      ],
    }))
    await renderCard()
    await screen.findByText('AI DRAFT')
    expect(screen.queryByText('Escalate immediately')).toBeNull()
    expect(screen.queryByText('Assess response to initial interventions')).toBeNull()
    // The section is gone rather than empty, and the withholding is STATED —
    // a section that quietly shrinks is indistinguishable from a model that
    // said less.
    expect(screen.queryByText(/What Sage would consider/i)).toBeNull()
    expect(screen.getByText(/withheld 2 further lines that restated the alert/i))
      .toBeInTheDocument()
  })

  it('keeps a chart-anchored step in the same channel', async () => {
    serve(readyAssessment())
    await renderCard()
    await screen.findByText('AI DRAFT')
    expect(screen.getByText('Hold the next metoprolol dose until reviewed')).toBeInTheDocument()
    expect(screen.queryByText(/Orb withheld/i)).toBeNull()
  })
})

/* ── F. THE VERDICT SURVIVES THE TIER GOING QUIET ─────────────────────────── */
describe('the advice tier: approve and decline through the transition', () => {
  const FLAGGED = {
    ...SILENT,
    outcome: 'flags' as const,
    model_called: true,
    candidates_n: 3,
    flags: [flag()],
  }

  /** Drive the component's own re-read, the way the server nudges it. */
  async function pushUpdate() {
    const entry = socketOn.mock.calls.find(c => c[0] === 'sage:assessment_update')
    if (!entry) throw new Error('the component never subscribed to assessment updates')
    await act(async () => {
      ;(entry[1] as (p: { patient_id: string }) => void)({ patient_id: 'P007' })
      await Promise.resolve()
    })
  }

  it('keeps both verdicts identical and reachable across silent → flagged', async () => {
    serve(readyAssessment({ patient_specific: SILENT }))
    await renderCard()
    await screen.findByText(SILENT_LINE)

    // SILENT: both verdicts, same control.
    let approve = screen.getByRole('button', { name: /approve draft/i })
    let decline = screen.getByRole('button', { name: /decline draft/i })
    expect(approve.className).toBe(decline.className)

    // The next alert on the same patient DOES have something to flag.
    serve(readyAssessment({ patient_specific: FLAGGED }))
    await pushUpdate()
    await screen.findByText(/falsely reassuring/i)
    expect(screen.queryByText(SILENT_LINE)).toBeNull()

    // FLAGGED: still both, still identical, still two taps each.
    approve = screen.getByRole('button', { name: /approve draft/i })
    decline = screen.getByRole('button', { name: /decline draft/i })
    expect(approve.className).toBe(decline.className)
    expect(screen.getAllByRole('button', { name: /decline draft/i })).toHaveLength(1)
  })

  it('records a verdict on a flagged draft and EXECUTES NOTHING', async () => {
    serve(readyAssessment({ patient_specific: FLAGGED }))
    transitionAssessment.mockResolvedValue({
      status: 'ok', assessment: readyAssessment({ state: 'approved', version: 3 }),
    })
    await renderCard()
    await screen.findByText(/falsely reassuring/i)

    fireEvent.click(screen.getByRole('button', { name: /approve draft/i }))
    fireEvent.click(screen.getByRole('button', { name: /matches my assessment/i }))
    await waitFor(() => expect(transitionAssessment).toHaveBeenCalledTimes(1))

    // THE LINE. A patient-specific caution is still a caution: agreeing that a
    // beta-blocker is masking a heart rate must not hold or give anything.
    expect(transitionSageAction).not.toHaveBeenCalled()
    const touched = apiFetch.mock.calls.map(c => String(c[0]))
    expect(touched.filter(p => p.includes('sage_actions'))).toEqual([])
    expect(touched.filter(p => p.includes('confirm'))).toEqual([])
    expect(touched.filter(p => p.includes('medication'))).toEqual([])
  })

  it('keeps the AI-drafted labelling in every advice state', async () => {
    const blocks = [
      ['silent', SILENT],
      ['flagged', FLAGGED],
      ['unchecked', { ...SILENT, outcome: 'failed' as const, message: 'The check did not run.' }],
    ] as const
    for (const [, block] of blocks) {
      serve(readyAssessment({ patient_specific: block }))
      const { unmount } = await renderCard()
      expect(await screen.findByText('AI DRAFT')).toBeInTheDocument()
      expect(screen.getByText(/not by a\s+clinician/i)).toBeInTheDocument()
      expect(screen.getByText(/Decision support only/i)).toBeInTheDocument()
      unmount()
    }
  })

  it('says which half is the model\'s and which half is the chart\'s', async () => {
    serve(readyAssessment({ patient_specific: FLAGGED }))
    await renderCard()
    await screen.findByText(/falsely reassuring/i)
    // The judgement is Sage's, the fact is Orb's. A block headed AI DRAFT that
    // did not separate them would let a model-invented drug read as a chart
    // entry.
    expect(screen.getByText(/Sage chose which of these chart facts matter here/i))
      .toBeInTheDocument()
    expect(screen.getByText(/the facts themselves are Orb's, read from this patient's record/i))
      .toBeInTheDocument()
  })
})

/* ── THE RULE ITSELF, WITHOUT A SCREEN ────────────────────────────────────── */
describe('patientSpecificView', () => {
  const wrap = (block: unknown) =>
    patientSpecificView(readyAssessment({
      patient_specific: block as SageAssessment['patient_specific'],
    }))

  it('renders nothing for a row that predates the tier', () => {
    expect(patientSpecificView(readyAssessment()).kind).toBe('absent')
    expect(patientSpecificView(null).kind).toBe('absent')
    expect(wrap(null).kind).toBe('absent')
  })

  it('calls both no-candidate outcomes silence, and records whether the model ran', () => {
    expect(wrap({ ...SILENT })).toEqual({ kind: 'silent', modelCalled: false })
    // `no_material` means the pre-screen DID produce candidates and the model
    // judged none of them material — the model ran, and the throughput number
    // has to be able to see that.
    expect(wrap({ ...SILENT, outcome: 'no_material', model_called: true, candidates_n: 4 }))
      .toEqual({ kind: 'silent', modelCalled: true })
  })

  it('never turns an unknown or failed outcome into silence', () => {
    for (const outcome of ['failed', 'timeout', '', 'FLAGS', undefined]) {
      const view = wrap({ ...SILENT, outcome })
      expect(view.kind).toBe('unchecked')
      if (view.kind === 'unchecked') expect(view.reason).toBe('failed')
    }
  })

  it('lets a surviving flag win over a contradicting outcome word', () => {
    // If the server labelled the row `no_material` but sent a caution with a
    // chart fact behind it, hiding the caution to match a bookkeeping field
    // would drop clinical content on the floor.
    const view = wrap({ ...SILENT, outcome: 'no_material', flags: [flag()] })
    expect(view.kind).toBe('flags')
    if (view.kind === 'flags') expect(view.flags).toHaveLength(1)
  })

  it('counts what it dropped, separately, by reason', () => {
    const view = wrap({
      ...SILENT,
      outcome: 'flags',
      flags: [
        flag(),
        flag({ key: 'a', caution: 'Escalate immediately' }),
        flag({ key: 'b', caution: 'Repeat observations' }),
        flag({ key: 'c', fact: '' }),
        flag({ key: 'd', caution: '  ' }),
      ],
    })
    expect(view.kind).toBe('flags')
    if (view.kind === 'flags') {
      expect(view.flags.map(f => f.key)).toEqual(['ps0'])
      expect(view.withheldGeneric).toBe(2)
      expect(view.withheldNoFact).toBe(2)
    }
  })

  it('falls to withheld — not to silence — when every flag is dropped', () => {
    const view = wrap({
      ...SILENT, outcome: 'flags', flags: [flag({ caution: 'Escalate immediately' })],
    })
    expect(view.kind).toBe('unchecked')
    if (view.kind === 'unchecked') {
      expect(view.reason).toBe('withheld')
      expect(view.withheldGeneric).toBe(1)
    }
  })

  it('survives a malformed flags field without inventing a finding', () => {
    expect(wrap({ ...SILENT, outcome: 'flags', flags: undefined }).kind).toBe('unchecked')
    expect(wrap({ ...SILENT, flags: 'not an array' }).kind).toBe('silent')
  })
})
