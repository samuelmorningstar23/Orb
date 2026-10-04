/**
 * SAGE'S DRAFTED ASSESSMENT — the clinician surface for a critical Vigil alert.
 *
 * ═══ THE ONE RULE THIS COMPONENT EXISTS TO OBEY ═══
 * THE ALERT NEVER WAITS FOR SAGE. Vitals-in to clinician-visible alert is 51 ms
 * and that number is the product. This component mounts BESIDE the alert, reads
 * a row that the backend wrote before the model ran, and renders whatever that
 * row currently says. It is mounted after the chart has already rendered, it
 * holds no loading gate over anything else, and when there is no draft it
 * renders NOTHING — the alert stands alone, which is the correct and common
 * outcome (a draft is written on a critical alert and nowhere else; with
 * ORB_SAGE_ASSESSMENT_ENABLED off, no row is written at all).
 *
 * ═══ WHY A SPINNER IS THE DANGEROUS DEFAULT HERE ═══
 * A spinner is a promise. On a deteriorating patient, a spinner that will never
 * resolve is worse than a blank space, because a clinician who believes an
 * assessment is thirty seconds away waits thirty seconds. So:
 *   - `generating` renders a skeleton AND says in words that the alert above is
 *     already complete and does not depend on this;
 *   - the server derives `unavailable`/`abandoned` for a row left `generating`
 *     past its stale window (a process killed mid-generation), so the terminal
 *     state arrives even when nothing of ours ran;
 *   - and STALL_CEILING_MS below is the backstop for the case the server cannot
 *     cover — our own polling silently stopping.
 *
 * ═══ WHAT MUST NEVER BE AMBIGUOUS ON THIS SURFACE ═══
 *   1. It is AI-DRAFTED, in every single state. The tag and the provenance line
 *      are rendered outside every state branch, deliberately, so that no future
 *      edit to one branch can drop them from that state alone.
 *   2. Approving EXECUTES NOTHING. The verdict endpoint records that a
 *      clinician read the reasoning and agrees with it. Anything Orb could
 *      actually do is a separate `sage_actions` card with its own confirmation,
 *      its own confirm lock and its own drug-safety gate, and this component
 *      never touches that endpoint — it says where the card is and stops.
 *   3. No retrieved guideline must not read as guideline-backed. When evidence
 *      is empty the server's `evidence_note` is rendered as PLAIN VISIBLE TEXT,
 *      never folded behind the same disclosure that hides a real source list.
 *   4. A FINISHED ASSESSMENT IS NEVER BURIED BY A LATER FAILED ONE. Every
 *      critical alert on a patient writes another row into the SAME case room,
 *      so "the assessment" is really "the newest row" — and on the ward a ready
 *      16:25 draft was replaced by a 16:26 sibling that failed, leaving the
 *      chart saying "No assessment was written" 101 seconds after Sage had
 *      written one. The newest row still governs the current state; an older
 *      row still awaiting a verdict is carried alongside it, labelled with the
 *      alert it belongs to, and stays reviewable. See `partitionAssessments`.
 *
 * ═══ THE ADVICE TIER IS USUALLY SILENT, AND SILENCE IS THE ANSWER ═══
 * Since 2026-08-15 the model's half says ONLY what depends on this patient's own
 * chart — a rate-controlling drug making a heart rate falsely reassuring, an
 * allergy ruling out the obvious agent, a creatinine that changes a fluid
 * decision. It no longer produces generic next steps, because it was measured
 * doing exactly that: two of its three steps restated the alert on a surface
 * that had already stated it deterministically.
 *
 * So on most alerts this tier has NOTHING to say, and "No patient-specific
 * factors identified" is a first-class, expected, common output. It is written
 * as a settled clinical finding rather than an empty slot, it carries no status
 * colour and no fill, and it is kept rigidly distinct from the check having
 * FAILED — see `patientSpecificView`. Those are opposite facts about a
 * deteriorating patient and this surface must never let them collapse.
 *
 * ═══ WHY DECLINING IS EXACTLY AS FAST AS APPROVING ═══
 * The declined draft is the most valuable row in the pilot dataset: a log that
 * only records successes reports a spectacular success rate. So the two verdicts
 * are the same control, the same size, the same colour, in the same order, and
 * both take exactly two taps (verdict → reason chip, which records immediately).
 * Neither is a primary button. If a future edit makes one of them heavier,
 * prettier or shorter than the other, it has put a thumb on the pilot's scale.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  getLatestAssessmentForPatient,
  listSageAssessments,
  transitionAssessment,
  type SageAssessment,
  type SageAssessmentConsideration,
  type SageAssessmentVerdict,
} from '../../api/sageAssessment'
import { socket } from '../../api/client'
import {
  isGenericAdviceLine,
  partitionAssessments,
  patientSpecificView,
  type PatientSpecificView,
} from './sageAssessmentView'
import News2Drivers from '../vigil/News2Drivers'
import { tier1DriverView } from '../vigil/news2DriverView'
import { useVisiblePolling } from '../../hooks/useVisiblePolling'
import { hasPermission, RELAY_EXECUTE_ACTION } from '../../lib/roles'
import { formatUTCDate, formatUTCTime } from '../../lib/datetime'
import { assertOnlineForWrite } from '../../lib/offline/writeGuard'
import { gotoTab } from '../../lib/orbBus'
import './SageAlertAssessment.css'

/** While the model is writing. Fast, because this is the one state where
 *  something really is on its way and the observed median is ~45s. */
const POLL_GENERATING_MS = 6_000
/** On a critical patient with no draft yet — the alert may have landed while
 *  the chart was already open. Slow, because this is speculative. */
const POLL_WATCHING_MS = 45_000

/** The backstop for OUR failure, not the server's.
 *
 *  The server resolves an abandoned `generating` row on read (default stale
 *  window 240s), so a killed appliance already stops spinning. What that cannot
 *  cover is this surface losing its poll — a paused timer, a socket that never
 *  fires, a tab restored from bfcache. 360s sits deliberately above the server's
 *  window: if the row still says `generating` this long after it opened, the
 *  honest reading is that we have lost track of it, not that it is nearly done.
 */
const STALL_CEILING_MS = 360_000

/** Trust tiers, most trusted first — the same vocabulary SageMessage uses. A
 *  clinician weighs "our SOP says" very differently from "a guideline somewhere
 *  says", so the tier is shown rather than averaged away. */
const TRUST_TIER_LABEL: Record<number, string> = {
  1: 'Hospital policy',
  2: 'Hospital formulary',
  3: 'External guideline',
  4: 'Reference',
}

/** How the model's words relate to a retrieved document. */
const ATTRIBUTION_LABEL: Record<string, string> = {
  model_cited: 'Cited by Sage',
  system_attributed: 'Matched by Orb',
  context_only: 'Retrieved, not used',
}

/** The header chip per state. `tone` is deliberately neutral everywhere except
 *  `unavailable`: an assessment awaiting review is not an emergency, and a
 *  saturated chip on every critical chart is how colour stops meaning anything. */
const STATE_CHIP: Record<string, { label: string; tone: 'neutral' | 'warn' }> = {
  generating: { label: 'Writing', tone: 'neutral' },
  ready: { label: 'Awaiting your review', tone: 'neutral' },
  unavailable: { label: 'No assessment', tone: 'warn' },
  approved: { label: 'Approved', tone: 'neutral' },
  declined: { label: 'Declined', tone: 'neutral' },
}

/** One tap records the verdict. Both lists are the same length and the same
 *  shape on purpose — see the file header. `null` is the free-text escape. */
const APPROVE_REASONS: readonly (string | null)[] = [
  'Matches my assessment',
  'Useful, acting on it',
  'Agree, already in hand',
  null,
]
const DECLINE_REASONS: readonly (string | null)[] = [
  'I disagree clinically',
  'Chart facts are wrong or missing',
  'Not relevant to this patient',
  null,
]

/** States that carry a readable four-section draft. */
const HAS_CONTENT = new Set(['ready', 'approved', 'declined'])

/** Has this draft been "writing" past the point where we should still believe
 *  it? Pure, so it is testable and cannot capture a stale closure. */
function isStalled(generating: boolean, createdAt: string | null | undefined, now: number): boolean {
  if (!generating || !createdAt) return false
  // SQLite timestamps can arrive without a zone; they are UTC by construction,
  // so an unzoned string is stamped Z rather than read as local time — which
  // would put the draft hours in the future or the past depending on where the
  // appliance is, and either way defeat the check.
  const iso = /[zZ]|[+-]\d{2}:?\d{2}$/.test(createdAt) ? createdAt : `${createdAt.replace(' ', 'T')}Z`
  const started = Date.parse(iso)
  return Number.isFinite(started) && now - started > STALL_CEILING_MS
}

interface Props {
  patientId: string
  /** The patient's CURRENT NEWS2 band. Used for one thing only: deciding
   *  whether a failed read is worth reporting (see `showReadFailure`). It never
   *  gates the draft itself — a patient who has improved since the alert still
   *  has an assessment somebody must resolve. */
  riskLevel?: string | null
  /** Passed from the panel rather than read from storage, matching the
   *  convention in VigilPanel: an explicit null holds nothing. */
  role?: string | null
  /** WHETHER THE TIER 1 BREAKDOWN IS ON SCREEN HERE.
   *
   *  The chart carries its own live decomposition of the current NEWS2, and
   *  Tier 1 is a richer decomposition of the observation the alert fired on.
   *  Both are deterministic and both are correct, but they describe DIFFERENT
   *  moments and can therefore print different numbers — so only one is allowed
   *  on screen at a time, and this is how the chart is told which. It must stay
   *  a report of what rendered, never a request: this component decides, the
   *  panel yields.
   *
   *  Pass a stable identity (a `useState` setter is one) — the effect that
   *  drives it depends on the function. */
  onTier1Shown?: (shown: boolean) => void
}

export default function SageAlertAssessment({ patientId, riskLevel, role, onTier1Shown }: Props) {
  const [assessment, setAssessment] = useState<SageAssessment | null>(null)
  /** Older drafts still awaiting a verdict, newest first. See
   *  `partitionAssessments` — these are the rows a newer failure used to hide. */
  const [carried, setCarried] = useState<SageAssessment[]>([])
  /** Carried drafts resolved in THIS session. Held separately so the verdict is
   *  confirmed on screen and the next unresolved draft can take its place
   *  immediately, without waiting for a re-read that may be minutes away. */
  const [carriedResolved, setCarriedResolved] = useState<SageAssessment[]>([])
  const [channelId, setChannelId] = useState<string | null>(null)
  const [readFailed, setReadFailed] = useState(false)
  /** Whether the first read has finished. Until it has, this renders nothing —
   *  never a placeholder, because at that moment we do not know a draft exists. */
  const [settled, setSettled] = useState(false)
  /** Ticked on each poll so the stall backstop below is re-evaluated without a
   *  per-second timer. Clinical numbers must never animate, and neither should
   *  this — the elapsed line moves at the polling rate, not at 60fps. */
  const [now, setNow] = useState(() => Date.now())

  /* The case-room lookup costs a /relay/channels round trip; the per-channel
     list does not. Caching the id means the first read pays for the lookup and
     every poll after it is one cheap indexed query. */
  const channelRef = useRef<string | null>(null)
  const alive = useRef(true)

  const load = useCallback(async () => {
    try {
      let rows: SageAssessment[]
      if (channelRef.current) {
        rows = await listSageAssessments(channelRef.current)
      } else {
        const found = await getLatestAssessmentForPatient(patientId)
        channelRef.current = found.channelId
        if (alive.current) setChannelId(found.channelId)
        // The whole list, because a newer failed row must not be able to hide an
        // older ready one. A caller that can only tell us about the newest row
        // degrades to exactly that and nothing is invented from it.
        rows = Array.isArray(found.rows) && found.rows.length > 0
          ? found.rows
          : (found.assessment ? [found.assessment] : [])
      }
      if (!alive.current) return
      const split = partitionAssessments(rows)
      setAssessment(split.current)
      setCarried(split.carried)
      setReadFailed(false)
    } catch {
      // A FAILED READ IS NOT "NO DRAFT". Whatever is already on screen stays on
      // screen; the failure is reported separately (see showReadFailure) rather
      // than blanking a draft a clinician may be part-way through reading.
      if (alive.current) setReadFailed(true)
    } finally {
      if (alive.current) {
        setSettled(true)
        setNow(Date.now())
      }
    }
  }, [patientId])

  useEffect(() => {
    alive.current = true
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    load()
    return () => { alive.current = false }
  }, [load])

  // The server nudges the CASE ROOM when a draft resolves. This surface is not
  // necessarily joined to that room, so the socket is a bonus and the poll is
  // the mechanism — a dropped event costs one polling interval, never a stuck
  // screen.
  useEffect(() => {
    const onUpdate = (payload?: { patient_id?: string }) => {
      if (!payload?.patient_id || payload.patient_id === patientId) load()
    }
    socket.on('sage:assessment_update', onUpdate)
    return () => { socket.off('sage:assessment_update', onUpdate) }
  }, [patientId, load])

  const state = assessment?.state ?? null
  const isGenerating = state === 'generating'
  const watching = !assessment && (riskLevel || '').toLowerCase() === 'critical'
  useVisiblePolling(load, POLL_GENERATING_MS, isGenerating)
  useVisiblePolling(load, POLL_WATCHING_MS, watching)

  /** The draft has been "writing" for longer than any plausible generation plus
   *  the server's own stale window. Something on our side stopped watching.
   *
   *  Deliberately NOT memoised: two string comparisons and a Date.parse are
   *  cheaper than the memo, and the React Compiler cannot preserve a manual
   *  memo whose real dependency (`assessment.created_at`) it infers differently
   *  from the written one — which turns the whole component into a compilation
   *  skip for no gain at all. */
  const stalled = isStalled(isGenerating, assessment?.created_at, now)

  const canResolve = hasPermission(RELAY_EXECUTE_ACTION, role)

  /** ── TIER 1, THE HALF THAT IS NEVER PENDING ──
   *  Deterministic, computed by Orb's engine BEFORE any generation started, and
   *  emitted on every payload whatever `state` says — so it is rendered in every
   *  state, including `unavailable`. An unavailable block is still an answer
   *  (the server says why, in its own words) and is rendered as one. */
  const tier1 = assessment?.tier1
  const tier1Shown = Boolean(tier1 && (tier1.available || tier1.message))

  /* Tell the chart, so it can stand its own live breakdown down. Reported after
     render, and reported as FALSE on unmount, so a chart that outlives this
     block never keeps hiding a panel nothing is replacing. */
  useEffect(() => {
    onTier1Shown?.(tier1Shown)
    return () => { onTier1Shown?.(false) }
  }, [tier1Shown, onTier1Shown])

  /** A carried draft resolved here. Kept in state rather than re-read, so the
   *  clinician sees their verdict land AND the next unresolved draft appears in
   *  the same beat — the read that would confirm it can be a poll away. */
  const onCarriedResolved = useCallback((next: SageAssessment) => {
    setCarriedResolved(prev => (prev.some(a => a.id === next.id) ? prev : [...prev, next]))
  }, [])

  // ── Nothing to say ────────────────────────────────────────────────────────
  // No draft and no reason to think one is coming: render nothing at all. The
  // alert stands alone, which is exactly what should happen.
  const showReadFailure = readFailed && !assessment
    && (riskLevel || '').toLowerCase() === 'critical'
  if (!settled) return null
  if (!assessment && !showReadFailure) return null

  const chip = state ? STATE_CHIP[state] : null

  /** ── THE CARRIED DRAFTS ──
   *  Older rows still awaiting a verdict, minus the ones resolved in this
   *  session. ONE of them is rendered in full and the rest are counted: a stack
   *  of complete drafts under a critical alert is its own wall, and a wall is
   *  what the clinician was already up against when the ready draft was hidden
   *  entirely. Resolving the surfaced one brings the next straight up. */
  const resolvedIds = new Set(carriedResolved.map(a => a.id))
  const carriedOpen = carried.filter(a => !resolvedIds.has(a.id))
  const carriedTop = carriedOpen.length > 0 ? carriedOpen[0] : null
  const carriedMore = Math.max(carriedOpen.length - 1, 0)

  /** The room a verdict is posted to. The looked-up id is the usual source; the
   *  row carries its own too, and either is authoritative — a draft we can read
   *  but cannot name a room for would otherwise lose its controls silently,
   *  which is the failure this whole change exists to remove. */
  const channelFor = (a: SageAssessment): string | null => channelId || a.channel_id

  return (
    <section className="sage-draft" aria-labelledby={`sage-draft-title-${patientId}`}>
      {/* ── Provenance. OUTSIDE every state branch, on purpose: this is the one
             piece of the surface that must survive an edit to any single state.
             It is also the first thing in the DOM, so a screen reader hears
             what this is before it hears a word of what it says. ── */}
      <header className="sage-draft__head">
        <span className="orb-tag sage-draft__ai-tag">AI DRAFT</span>
        <h3 className="sage-draft__title" id={`sage-draft-title-${patientId}`}>
          Sage's assessment of this alert
        </h3>
        {chip && (
          <span
            className={`sage-draft__chip${chip.tone === 'warn' ? ' sage-draft__chip--warn' : ''}`}
          >
            {chip.label}
          </span>
        )}
      </header>
      <p className="sage-draft__provenance">
        Written by Orb's clinical model{assessment?.model ? ` (${assessment.model})` : ''}, not by a
        clinician. Decision support only.
        {/* WHEN, not just what. An assessment is a snapshot of one alert; read
            two hours later on a patient who has since been treated, an undated
            draft reads as current advice. The time it was opened is the honest
            stamp — a draft that is still `generating` has no other. */}
        {assessment?.created_at && (
          <> Drafted {formatUTCTime(assessment.created_at)} on{' '}
            {formatUTCDate(assessment.created_at)}.</>
        )}
      </p>

      {showReadFailure && (
        <div className="orb-state orb-state--error sage-draft__block" role="status">
          <div className="orb-state__content">
            <div className="orb-state__title">Orb could not check for an assessment</div>
            <div className="orb-state__detail">
              This is a display failure, not an all-clear. The alert and the vitals above are
              unaffected. Sage may or may not have drafted an assessment for this patient.
            </div>
          </div>
        </div>
      )}

      {assessment && (
        <>
          {/* ── The deterministic score, stated as Orb's, never the model's ──
                 Its three driver STRINGS are suppressed when the Tier 1 block
                 below is rendering: those are the same three parameters, capped
                 at three, with the movement removed. Two driver lists an inch
                 apart, one of them a truncation of the other, is the reader
                 being asked which to believe. */}
          <News2Line assessment={assessment} showDrivers={!tier1Shown} />

          {/* ── TIER 1 — the drivers and the movement, above everything the
                 model had any part in.
                 It is here, and above the "Sage is writing" note, because it is
                 already finished when that note first paints: it costs 33-240 ms
                 with no model in the path, it lands on 100% of alerts, and it
                 survives every way generation can fail. A surface that put it
                 below the pending half would be telling a clinician to wait for
                 something they already have. ── */}
          {tier1 && tier1Shown && (
            <News2Drivers
              view={tier1DriverView(tier1, assessment.created_at)}
              id={`current-${patientId}`}
            />
          )}

          {/* ── THE ADVICE TIER — patient-specific only, and usually silent.
                 It sits HERE, directly under the numbers and above the prose,
                 for the same reason Tier 1 sits above the pending half: a
                 caution that a heart rate of 78 is falsely reassuring changes
                 how the block above it reads, so it belongs beside those
                 numbers rather than three paragraphs below them. Most of the
                 time it is one quiet settled line, and it is rendered in every
                 state the payload carries it in — `no_candidates` is known
                 deterministically, before any model runs, so there is nothing
                 to wait for and no honest reason to hide it behind a state. ── */}
          <PatientSpecific view={patientSpecificView(assessment)} />

          {isGenerating && !stalled && (
            <div className="sage-draft__block sage-draft__generating" role="status">
              <div className="sage-draft__generating-line">Sage is writing an assessment.</div>
              <div className="sage-draft__generating-note">
                The alert above is already complete and does not depend on this. It usually takes
                under a minute; if it fails, this box will say so.
              </div>
              {/* Skeleton, not a spinner: the geometry matches the loaded draft,
                  and it animates BACKGROUND rather than opacity (house rule). */}
              <div className="orb-skeleton" aria-hidden="true">
                <div className="orb-skeleton__bar" style={{ width: '92%' }} />
                <div className="orb-skeleton__bar" style={{ width: '78%' }} />
                <div className="orb-skeleton__bar" style={{ width: '85%' }} />
              </div>
            </div>
          )}

          {isGenerating && stalled && (
            <div className="orb-state orb-state--error sage-draft__block" role="status">
              <div className="orb-state__content">
                <div className="orb-state__title">No assessment has arrived</div>
                <div className="orb-state__detail">
                  Sage has been writing for several minutes. Treat this alert as having no
                  assessment. Do not wait for one.
                </div>
              </div>
            </div>
          )}

          {state === 'unavailable' && (
            <div className="sage-draft__block sage-draft__unavailable" role="status">
              <div className="sage-draft__unavailable-title">No assessment was written</div>
              {/* VERBATIM from the server. The API, the tests, the voice lane
                  and the handover sheet share one wording per reason so the
                  ward cannot be told three stories about the same failure. */}
              <p className="sage-draft__unavailable-why">
                {assessment.unavailable_message
                  || 'Sage could not write an assessment for this alert.'}
              </p>
              {assessment.fallback_text && (
                <p className="sage-draft__fallback">
                  <span className="sage-draft__fallback-label">Standing guidance</span>
                  {assessment.fallback_text}
                  <span className="sage-draft__fallback-note">
                    {' '}This is Orb's fixed degraded-mode line, the same sentence for every alert,
                    not an assessment of this patient.
                  </span>
                </p>
              )}
            </div>
          )}

          {state && HAS_CONTENT.has(state) && <DraftBody assessment={assessment} />}

          {/* ── The verdict.
                 The disclaimer is rendered VERBATIM and immediately above the
                 two buttons, because that is where somebody about to press one
                 is looking. ── */}
          {state === 'ready' && canResolve && channelFor(assessment) && (
            <VerdictPanel
              assessment={assessment}
              channelId={channelFor(assessment) as string}
              inputId={`sage-draft-reason-${patientId}`}
              onResolved={setAssessment}
              onConflict={load}
            />
          )}

          {state === 'ready' && !canResolve && (
            <p className="sage-draft__no-permission">
              A doctor records the verdict on this draft. Nothing here has been ordered, charted or
              administered.
            </p>
          )}

          {(state === 'approved' || state === 'declined') && (
            <p className="sage-draft__resolved" role="status">
              <strong>
                {state === 'approved'
                  ? 'A clinician approved this draft.'
                  : 'A clinician declined this draft.'}
              </strong>{' '}
              {assessment.disclaimer}
            </p>
          )}
        </>
      )}

      {/* ── CARRIED OVER FROM AN EARLIER ALERT ──
             Verdicts recorded here in this session, confirmed in place. Without
             this the draft simply vanishes when it is resolved, which on a
             surface whose whole problem was drafts vanishing is the last thing
             it should do. ── */}
      {carriedResolved.map(a => (
        <p className="sage-draft__carried-resolved" role="status" key={a.id}>
          <strong>
            {a.state === 'approved'
              ? `A clinician approved the assessment from the ${formatUTCTime(a.created_at)} alert.`
              : `A clinician declined the assessment from the ${formatUTCTime(a.created_at)} alert.`}
          </strong>{' '}
          {a.disclaimer}
        </p>
      ))}

      {carriedTop && (
        <section
          className="sage-draft__carried"
          aria-labelledby={`sage-draft-carried-title-${patientId}`}
        >
          <header className="sage-draft__head">
            {/* The provenance label is repeated here in full, not inherited from
                the block above: this is a different document, and a reader who
                scrolls straight to it must meet the same two facts — AI-drafted,
                and which alert it belongs to. */}
            <span className="orb-tag sage-draft__ai-tag">AI DRAFT</span>
            <h4 className="sage-draft__carried-title" id={`sage-draft-carried-title-${patientId}`}>
              An assessment from the {formatUTCTime(carriedTop.created_at)} alert is still awaiting
              your review
            </h4>
            <span className="sage-draft__chip">Awaiting your review</span>
          </header>
          <p className="sage-draft__provenance">
            Written by Orb's clinical model{carriedTop.model ? ` (${carriedTop.model})` : ''}, not
            by a clinician. Decision support only. Drafted{' '}
            {formatUTCTime(carriedTop.created_at)} on {formatUTCDate(carriedTop.created_at)}, for an
            earlier alert than the one above. Nobody has approved or declined it.
          </p>

          {carriedMore > 0 && (
            <p className="sage-draft__carried-more">
              {carriedMore === 1
                ? '1 further assessment for this patient is also awaiting review.'
                : `${carriedMore} further assessments for this patient are also awaiting review.`}
              {' '}The next one takes this one's place when you resolve it.
            </p>
          )}

          {/* NO SECOND TIER 1 BLOCK HERE, deliberately. The carried draft has
              one — an older snapshot of the same patient — and two
              decompositions of two different moments, stacked, is a reader
              being asked to work out which score is which on a deteriorating
              patient. The carried draft keeps its pinned score (below) and its
              advice, which is what a verdict is actually recorded on. */}
          <News2Line assessment={carriedTop} />
          {/* The carried draft keeps its OWN advice tier — one implementation,
              two call sites, the same reason VerdictPanel is not duplicated. A
              clinician is being asked to approve or decline THIS draft, and a
              draft reachable only in a cut-down form is one nobody can honestly
              rule on. */}
          <PatientSpecific view={patientSpecificView(carriedTop)} />
          <DraftBody assessment={carriedTop} />

          {canResolve && channelFor(carriedTop) ? (
            <VerdictPanel
              assessment={carriedTop}
              channelId={channelFor(carriedTop) as string}
              inputId={`sage-draft-carried-reason-${patientId}`}
              onResolved={onCarriedResolved}
              onConflict={load}
            />
          ) : (
            <p className="sage-draft__no-permission">
              A doctor records the verdict on this draft. Nothing here has been ordered, charted or
              administered.
            </p>
          )}
        </section>
      )}
    </section>
  )
}

/** Orb's OWN NEWS2 for the alert this draft was written on.
 *
 *  Deterministic, computed by Orb's engine, pinned into the prompt as
 *  authoritative and never recalculated by the model — so it is displayed as a
 *  fact about Orb, and the note saying so travels with it. One renderer, so a
 *  carried draft cannot end up stating its score with different words from the
 *  current one.
 */
function News2Line({ assessment, showDrivers = true }: {
  assessment: SageAssessment
  /** The three driver strings. Off when the full Tier 1 breakdown is rendering
   *  below this line — it is the same content, uncapped and with the movement
   *  the strings drop. The SCORE always renders. */
  showDrivers?: boolean
}) {
  if (typeof assessment.news2 !== 'number') return null
  return (
    <div className="sage-draft__news2">
      <span className="sage-draft__news2-value metric-mono">NEWS2 {assessment.news2}</span>
      {assessment.news2_level && (
        <span className="sage-draft__news2-band">{assessment.news2_level}</span>
      )}
      <span className="sage-draft__news2-note">
        Orb's own score at the time of the alert. Sage is given this figure and does not
        recalculate it.
      </span>
      {showDrivers && assessment.news2_drivers.length > 0 && (
        <ul className="sage-draft__drivers">
          {assessment.news2_drivers.map(d => <li key={d}>{d}</li>)}
        </ul>
      )}
    </div>
  )
}

/** THE ADVICE TIER, WHOSE COMMONEST OUTPUT IS SILENCE.
 *
 *  ═══ SILENCE HAS TO READ AS A FINDING, NOT AS A BROKEN FEATURE ═══
 *  A tier that always finds something to say is padding, and padding is what
 *  made the last one decoration. So "No patient-specific factors identified" is
 *  written as a settled clinical statement — Orb looked, here is what it looked
 *  at, here is what it found — in the register of the degraded-mode line beside
 *  it, which is the most trustworthy copy in the product precisely because it
 *  says exactly what it is and claims nothing more.
 *
 *  ═══ AND IT MUST NOT READ AS REASSURANCE ═══
 *  "Nothing patient-specific to flag" is not "this patient is fine". The block
 *  says so in words, because a quiet green-feeling line under a NEWS2 19 is a
 *  worse failure than no line at all. It is also why this state carries NO
 *  status colour and no fill: a tier with no warning to give must not occupy the
 *  space or the colour of a warning.
 *
 *  ═══ THE FACT IS SEPARATE FROM THE JUDGEMENT, VISUALLY ═══
 *  The judgement is Sage's; the fact is the chart's. They are drawn as two
 *  different things so a clinician can verify "on metoprolol 25 mg BD" without
 *  reading a word of the reasoning. A checkable claim beats a confident one.
 *
 *  ═══ CAUTIONS, NOT ORDERS ═══
 *  Nothing here is a control. There is no button, no confirm, no "act on this"
 *  — approving the draft that contains this executes nothing, and the block
 *  says that too.
 */
function PatientSpecific({ view }: { view: PatientSpecificView }) {
  // No block on the payload: a row written before this tier existed. Rendering
  // an empty panel here would invent a finding out of a schema gap.
  if (view.kind === 'absent') return null

  const title = 'What this patient\'s chart changes'

  if (view.kind === 'silent') {
    return (
      <section className="sage-ps sage-ps--silent" aria-label={title} data-state="silent">
        <h4 className="sage-ps__title">{title}</h4>
        {/* THE SENTENCE. Stated flat and finished — no chip, no colour, no
            ellipsis, nothing that could be read as still arriving. */}
        <p className="sage-ps__statement">No patient-specific factors identified.</p>
        <p className="sage-ps__detail">
          Orb checked this patient's medications, allergies, active problems and recent results
          against this deterioration. Nothing on the chart changes how the numbers above should be
          read. This is a finding, not a gap. On most alerts there is nothing to flag.
        </p>
        {/* The one thing silence must never be mistaken for. */}
        <p className="sage-ps__caveat">
          It is not a statement that the patient is well. The score and its drivers above are what
          say how sick they are.
        </p>
      </section>
    )
  }

  if (view.kind === 'unchecked') {
    return (
      <section
        className="sage-ps sage-ps--unchecked"
        aria-label={title}
        data-state={`unchecked-${view.reason}`}
        role="status"
      >
        <h4 className="sage-ps__title">{title}</h4>
        <p className="sage-ps__statement">
          {view.reason === 'failed'
            ? 'Orb could not complete the patient-specific check.'
            : 'Sage\'s patient-specific note was withheld.'}
        </p>
        {/* The server's wording when it has one — same rule as
            `unavailable_message`: one failure, one sentence, everywhere. */}
        {view.reason === 'failed' && view.message && (
          <p className="sage-ps__detail">{view.message}</p>
        )}
        {view.reason === 'withheld' && (
          <p className="sage-ps__detail">
            Every line it produced was dropped here:{' '}
            {withheldPhrase(view.withheldGeneric, view.withheldNoFact)}.
          </p>
        )}
        {/* THE WHOLE POINT OF SEPARATING THIS FROM SILENCE. */}
        <p className="sage-ps__caveat">
          Treat this alert as <strong>unchecked</strong> for patient-specific factors, not as
          clear. Orb has not looked at this patient's chart for anything that would change how these
          numbers read.
        </p>
      </section>
    )
  }

  const n = view.flags.length
  return (
    <section className="sage-ps sage-ps--flags" aria-label={title} data-state="flags">
      <h4 className="sage-ps__title">{title}</h4>
      <p className="sage-ps__statement">
        {n === 1
          ? 'One thing on this patient\'s chart changes how this deterioration reads.'
          : `${n} things on this patient's chart change how this deterioration reads.`}
      </p>
      <ul className="sage-ps__list">
        {view.flags.map(f => (
          <li className="sage-ps__flag" key={f.key || f.fact}>
            {/* THE JUDGEMENT — Sage's. */}
            <span className="sage-ps__caution">{f.caution}</span>
            {/* THE FACT — the chart's, boxed and labelled so it reads as
                something to check rather than something to believe. */}
            <span className="sage-ps__fact">
              <span className="sage-ps__fact-label">From this chart</span>
              <span className="sage-ps__fact-text">{f.fact}</span>
              {(f.fact_source || f.fact_recorded || f.relates_to) && (
                <span className="sage-ps__fact-meta">
                  {[f.fact_source, f.fact_recorded, f.relates_to].filter(Boolean).join(' · ')}
                </span>
              )}
            </span>
          </li>
        ))}
      </ul>
      {(view.withheldGeneric > 0 || view.withheldNoFact > 0) && (
        <p className="sage-ps__withheld">
          Orb withheld {withheldPhrase(view.withheldGeneric, view.withheldNoFact)}.
        </p>
      )}
      <p className="sage-ps__footer">
        Cautions, not orders. Sage chose which of these chart facts matter here; the facts
        themselves are Orb's, read from this patient's record. Nothing has been ordered, charted or
        administered, and approving this draft carries nothing out.
      </p>
    </section>
  )
}

/** How many lines were dropped and why, in one clause. Stated rather than
 *  swallowed: a filter that silently eats output is indistinguishable from a
 *  tier that produced none, which is the one confusion this surface exists to
 *  prevent. */
function withheldPhrase(generic: number, noFact: number): string {
  const parts: string[] = []
  if (generic > 0) {
    parts.push(`${generic} line${generic === 1 ? '' : 's'} that restated the alert`)
  }
  if (noFact > 0) {
    parts.push(`${noFact} line${noFact === 1 ? '' : 's'} that named no chart fact`)
  }
  return parts.join(' and ')
}

/** Does this note assert that Orb LOOKED and found nothing, or that it could
 *  not look? Two opposite facts about a patient, and the block's colour and its
 *  caveat both turn on which one it is.
 *
 *  Matched on the server's own opening clause (`NO_FACTORS_NOTES`), and
 *  DELIBERATELY FAIL-SAFE: anything this does not recognise is treated as "not
 *  checked", so a note this client has never seen gets the cautious treatment
 *  rather than the calm one. The failure direction matters more than the match
 *  rate — over-warning is recoverable, a false all-clear is not.
 */
function noteIsANegativeFinding(note: string): boolean {
  return /^\s*No patient-specific factors identified/i.test(note)
}

/** THE VERDICT OF A TIER THAT FOUND NOTHING, said out loud.
 *
 *  Reuses the `sage-ps` shell that `PatientSpecific` was built for, so the
 *  silent state looks the same wherever it is derived from — and carries NO
 *  status colour when the check ran cleanly, because a tier with no warning to
 *  give must not occupy the colour of a warning. The one case that does earn
 *  the warn rule is the check that could not run at all.
 */
function NoFactorsVerdict({ note }: { note: string }) {
  const checked = noteIsANegativeFinding(note)
  return (
    <section
      className={`sage-ps ${checked ? 'sage-ps--silent' : 'sage-ps--unchecked'}`}
      aria-label="What this patient's chart changes"
      data-state={checked ? 'silent' : 'unchecked-failed'}
    >
      <h4 className="sage-ps__title">What this patient's chart changes</h4>
      <p className="sage-ps__statement">{note}</p>
      {/* The one thing silence must never be mistaken for. Omitted when Orb
          could not look: the server's own sentence already carries the harder
          warning there ("Do not read this as 'there are none'"), and stacking a
          second caveat on it would blunt it. */}
      {checked && (
        <p className="sage-ps__caveat">
          It is not a statement that the patient is well. The score and its drivers above are what
          say how sick they are.
        </p>
      )}
    </section>
  )
}

/** The four sections, in the order a clinician needs them.
 *
 *  Rendered identically for the current draft and for one carried over from an
 *  earlier alert: a draft that is only reachable in a cut-down form is a draft
 *  nobody can honestly approve, and approving it is exactly what this surface is
 *  asking somebody to do.
 */
function DraftBody({ assessment }: { assessment: SageAssessment }) {
  const [evidenceOpen, setEvidenceOpen] = useState(false)

  /* ── THE SAME BAN, ON THE OLDER CHANNEL ──
     `considerations` is where the generic next steps used to arrive, and it is
     the field a partially-migrated server can still fill. A line that would be
     true of any patient with these numbers is banned wherever it appears on
     this surface, not only in the new block — otherwise the ruling holds in one
     lane and leaks in the other, which is exactly how the anti-hallucination
     strip failed before. Withheld lines are COUNTED and stated, never
     swallowed: a section that quietly shrinks is indistinguishable from a model
     that said less. */
  const steps = assessment.considerations.filter(c => !isGenericAdviceLine(c.step))
  const withheldSteps = assessment.considerations.length - steps.length

  return (
    <div className="sage-draft__body">
      {/* ── 1. The interpretation, first and largest. Ten seconds at a
             deteriorating bedside buys you this paragraph and nothing else, so
             it is what the paragraph has to be. ── */}
      {assessment.whats_happening && (
        <p className="sage-draft__summary">{assessment.whats_happening}</p>
      )}

      {/* ── 2. Considerations ── */}
      {steps.length > 0 && (
        <div className="sage-draft__section">
          <h4 className="sage-draft__section-title">What Sage would consider</h4>
          <ul className="sage-draft__considerations">
            {steps.map(c => (
              <Consideration key={c.key} item={c} />
            ))}
          </ul>
          {/* Set only when the list was cut short by the generation budget. A
              truncated list must not be able to read as a finished one. */}
          {assessment.advice_note && (
            <p className="sage-draft__advice-note-line">{assessment.advice_note}</p>
          )}
        </div>
      )}

      {/* ── 2b. THE TIER SPOKE AND SAID "NOTHING". ──
          The verdict of a tier whose commonest output is silence has to be ON
          SCREEN. `PatientSpecific` above renders the designed block from a
          `patient_specific` payload the server does not yet emit, so without
          this branch a no-factors draft renders Tier 1 and then NOTHING — the
          reader cannot tell a finished check that found nothing from a feature
          that failed to load, which is the one confusion this tier exists to
          prevent.

          THE SENTENCE IS THE SERVER'S, VERBATIM. Orb's own three notes already
          distinguish "checked and found nothing" from "could not check"
          (NO_FACTORS_NOTES); re-authoring them here would be a second copy of a
          clinical claim, and a client that writes its own would eventually
          write the wrong one. One failure, one sentence, everywhere.

          SAFE BECAUSE THE SHAPE IS UNAMBIGUOUS: the server sets `advice_note`
          with an EMPTY considerations list only on the no-factors path
          (`_no_factors_draft`); the truncation note is only ever attached to a
          list that has steps in it. */}
      {steps.length === 0 && assessment.advice_note && (
        <NoFactorsVerdict note={assessment.advice_note} />
      )}

      {withheldSteps > 0 && (
        <p className="sage-draft__withheld">
          Orb withheld {withheldSteps} further line{withheldSteps === 1 ? '' : 's'} that restated
          the alert rather than saying anything about this patient.
        </p>
      )}

      {/* ── 3. Watch items ── */}
      {assessment.watch_items.length > 0 && (
        <div className="sage-draft__section">
          <h4 className="sage-draft__section-title">Watch for</h4>
          <ul className="sage-draft__watch">
            {assessment.watch_items.map(w => <li key={w}>{w}</li>)}
          </ul>
        </div>
      )}

      {/* ── 4. Evidence.
             Present: one tap away, because it is what you check AFTER you have
             decided whether the interpretation is plausible.
             Absent: NOT behind the tap. An empty disclosure control labelled
             "Evidence" reads as "there is evidence here"; the server's sentence
             saying there is none has to be on screen without being asked for. ── */}
      {assessment.evidence.length > 0 ? (
        <div className="sage-draft__section">
          <button
            type="button"
            className="sage-draft__evidence-toggle"
            aria-expanded={evidenceOpen}
            onClick={() => setEvidenceOpen(o => !o)}
          >
            {evidenceOpen ? 'Hide' : 'Show'} evidence ({assessment.evidence.length})
          </button>
          {evidenceOpen && (
            <ul className="sage-draft__evidence">
              {assessment.evidence.map((e, i) => (
                /* Composite key: two chart facts from one rule share a
                   document_id ("chart:<rule>"), and drafts stored before the
                   server deduped guideline chunks can repeat one. */
                <li key={`${e.document_id ?? 'doc'}:${i}`} className="sage-draft__evidence-item">
                  <span className="sage-draft__evidence-title">
                    {e.title || e.document_id || 'Untitled document'}
                  </span>
                  <span className="sage-draft__evidence-meta">
                    {(typeof e.trust_tier === 'number' && TRUST_TIER_LABEL[e.trust_tier])
                      || e.source || 'Source'}
                    {' · '}
                    {ATTRIBUTION_LABEL[String(e.attribution)] || 'Retrieved'}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : assessment.evidence_note && steps.length > 0 ? (
        /* GATED ON THERE BEING STEPS. The server's sentence is "…These next
           steps are model reasoning over the chart alone", and it is emitted
           whenever a ready draft cites no guideline — including a no-factors
           draft, which has no next steps at all. Printing it there tells a
           clinician there is reasoning on screen to weigh when there is none,
           directly under a block that just said Orb found nothing. */
        <p className="sage-draft__no-evidence">{assessment.evidence_note}</p>
      ) : null}

      {assessment.confidence && (
        <p className="sage-draft__confidence">
          Sage's confidence in this draft: <strong>{assessment.confidence}</strong>
        </p>
      )}
    </div>
  )
}

/** The verdict, and the line it must never cross.
 *
 *  ONE implementation, used by the current draft and by a carried-over one. The
 *  symmetry this component exists to hold — same control, same size, same
 *  colour, same order, two taps each — is a property of the pilot dataset, not
 *  of a screen position, so a second copy of these buttons written for the
 *  carried draft would be a second place for that symmetry to rot.
 *
 *  `onResolved` hands the transitioned row back rather than triggering a re-read:
 *  the caller knows where that row lives on its surface, and the read that would
 *  confirm it can be a polling interval away.
 */
function VerdictPanel({ assessment, channelId, inputId, onResolved, onConflict }: {
  assessment: SageAssessment
  channelId: string
  /** Unique per panel — two verdict panels can be on screen at once, and a
   *  duplicated id would point both labels at the same input. */
  inputId: string
  onResolved: (next: SageAssessment) => void
  onConflict: () => void
}) {
  const [verdictOpen, setVerdictOpen] = useState<SageAssessmentVerdict | null>(null)
  const [freeText, setFreeText] = useState('')
  const [freeTextOpen, setFreeTextOpen] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [verdictError, setVerdictError] = useState<string | null>(null)
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])

  const submitVerdict = useCallback(async (verdict: SageAssessmentVerdict, reason: string | null) => {
    setSubmitting(true)
    setVerdictError(null)
    try {
      assertOnlineForWrite('record your verdict on this draft')
      const res = await transitionAssessment(
        channelId, assessment.id, verdict, assessment.version, reason,
      )
      if (!alive.current) return
      setVerdictOpen(null)
      setFreeTextOpen(false)
      setFreeText('')
      onResolved(res.assessment)
    } catch (err) {
      if (!alive.current) return
      const message = err instanceof Error ? err.message : 'Could not record your verdict.'
      setVerdictError(message)
      // A 409 means somebody else resolved it while this was open. Re-read so
      // the surface shows the verdict that actually landed rather than offering
      // to overwrite it.
      if ((err as { status?: number })?.status === 409) onConflict()
    } finally {
      if (alive.current) setSubmitting(false)
    }
  }, [assessment.id, assessment.version, channelId, onResolved, onConflict])

  return (
    <div className="sage-draft__verdict">
      {/* The disclaimer is rendered VERBATIM and immediately above the two
          buttons, because that is where somebody about to press one is looking. */}
      <p className="sage-draft__disclaimer">{assessment.disclaimer}</p>
      {verdictOpen === null ? (
        <div className="sage-draft__verdict-buttons">
          {/* Identical class, identical order, identical everything. */}
          <button
            type="button"
            className="btn-glass sage-draft__verdict-btn"
            onClick={() => { setVerdictError(null); setVerdictOpen('approved') }}
          >
            Approve draft
          </button>
          <button
            type="button"
            className="btn-glass sage-draft__verdict-btn"
            onClick={() => { setVerdictError(null); setVerdictOpen('declined') }}
          >
            Decline draft
          </button>
        </div>
      ) : (
        <div className="sage-draft__reasons">
          <div className="sage-draft__reasons-prompt">
            {verdictOpen === 'approved'
              ? 'Approving. Why? (one tap records it)'
              : 'Declining. Why? (one tap records it)'}
          </div>
          <div className="sage-draft__reason-chips">
            {(verdictOpen === 'approved' ? APPROVE_REASONS : DECLINE_REASONS).map(r => (
              <button
                key={r ?? '__other'}
                type="button"
                className="sage-draft__reason-chip"
                disabled={submitting}
                onClick={() => {
                  if (r === null) { setFreeTextOpen(true); return }
                  submitVerdict(verdictOpen, r)
                }}
              >
                {r ?? 'Other…'}
              </button>
            ))}
          </div>
          {freeTextOpen && (
            <div className="sage-draft__free">
              <label className="sage-draft__free-label" htmlFor={inputId}>
                In your words
              </label>
              <input
                id={inputId}
                className="sage-draft__free-input"
                value={freeText}
                onChange={e => setFreeText(e.target.value)}
                maxLength={280}
                autoComplete="off"
              />
              <button
                type="button"
                className="btn-glass sage-draft__free-record"
                disabled={submitting || freeText.trim().length === 0}
                onClick={() => submitVerdict(verdictOpen, freeText.trim())}
              >
                Record
              </button>
            </div>
          )}
          <button
            type="button"
            className="btn-ghost sage-draft__cancel"
            disabled={submitting}
            onClick={() => { setVerdictOpen(null); setFreeTextOpen(false); setFreeText('') }}
          >
            Cancel
          </button>
        </div>
      )}
      {verdictError && (
        <p className="sage-draft__verdict-error" role="alert">{verdictError}</p>
      )}
    </div>
  )
}

/** One considered step.
 *
 *  `orb_can_act` is the server's statement that a `sage_actions` CARD really
 *  exists, set from the created row and never from the model's claim about
 *  itself. So this renders as a fact about Orb — and, critically, as a POINTER
 *  to where the card is confirmed, not as a confirm button. Putting a confirm
 *  here would put a second door onto a path whose only door is meant to be the
 *  card route, with its per-action lock and its drug-safety gate.
 */
function Consideration({ item }: { item: SageAssessmentConsideration }) {
  return (
    <li className="sage-draft__consideration">
      <span className="sage-draft__step">{item.step}</span>
      {item.why && <span className="sage-draft__why">{item.why}</span>}
      {item.orb_can_act ? (
        <span className="sage-draft__card-note">
          Orb has put this in the patient's case room as a separate card. Confirming it there is its
          own step, with its own safety checks.{' '}
          <button type="button" className="sage-draft__goto" onClick={() => gotoTab('relay')}>
            Open Messages
          </button>
        </span>
      ) : (
        <span className="sage-draft__advice-note">Advice only: Orb cannot carry this out.</span>
      )}
    </li>
  )
}
