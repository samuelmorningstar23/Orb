/**
 * ORB — WARD ROUND MODE
 *
 * A doctor at a bed with a tablet in one hand and roughly 90 seconds per
 * patient. Everything below follows from that sentence.
 *
 * ORB DRIVES THE SEQUENCE. The round is a queue in walk order, not a list to
 * scroll. Orb says which bed is next, tracks seen / skipped / not-reached, and
 * can hand on what was not finished. It resumes, because a ward round is
 * interrupted constantly.
 *
 * ONE SCREEN, NO TABS. Overnight events, current issues and today's decisions
 * sit side by side. Nothing clinical on the screen is computed here: NEWS2 and
 * its drivers, observation and medication timing, pharmacist verification and
 * Sage insights all arrive already scored from the server (see
 * components/round/roundSummary.ts). The panel classifies and orders.
 *
 * IT USES ONLY EXISTING, DEPARTMENT-SCOPED ENDPOINTS. The ward board
 * (/nurse/board) and the patient board (/vigil/patients) both derive their
 * cohort from the authenticated user via permissions.department_scope — this
 * client sends no scope, so it cannot widen one. Both are PHI-read-audited
 * server-side, as is each patient story it opens.
 *
 * WHAT IT PERSISTS, AND WHY SO LITTLE. Round progress survives a reload
 * (patient IDs plus a seen/skipped mark, nothing else) and any other
 * clinician's round is erased from the device on mount — a ward tablet is
 * shared, and which patients a named doctor visited is itself disclosure.
 * Dictated plans are held in memory ONLY: that is free clinical text about a
 * named patient, and it does not belong on a shared tablet's disk. The cost is
 * stated honestly to the user — the hand-on screen warns about unsaved plans.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  addClinicalNote,
  getPatients,
  getPatientStory,
  getSageInsights,
  getUser,
  type Patient,
  type SageInsight,
  type StoryEvent,
} from '../../api/client'
import { assertOnlineForWrite } from '../../lib/offline/writeGuard'
import { useDataChanged } from '../../hooks/useDataChanged'
import { getShiftBoard, type NursePatientCard } from '../../api/nurse'
import {
  getReasonedAssessment,
  prepareReasonedAssessment,
  type IpassReasonedAssessment,
} from '../../api/ipass'
import { ASSESSMENT_RECHECK_MS } from '../../components/handoff/reasonedAssessmentCopy'
import RoundDictation, { type SaveState } from '../../components/round/RoundDictation'
import RoundHandoff from '../../components/round/RoundHandoff'
import RoundQueueRail from '../../components/round/RoundQueueRail'
import RoundSummaryView, { type LoadPhase } from '../../components/round/RoundSummaryView'
import { RoundDegraded, RoundEmpty, RoundError, RoundLoading } from '../../components/round/RoundStates'
import {
  handoffList,
  loadRoundState,
  mergeRoster,
  nextIncomplete,
  previousInQueue,
  progressOf,
  purgeForeignRoundState,
  reconcileStatuses,
  saveRoundState,
  todayKey,
  type RoundStatus,
} from '../../components/round/roundQueue'
import { documentedToday } from '../../components/round/roundSummary'
import '../../components/round/Round.css'

interface StoryEntry {
  phase: LoadPhase
  events: StoryEvent[]
  error: string | null
}

/** Doc type on the note this panel writes. Kept distinct from a generic
 *  progress note so a ward-round plan is identifiable in the chart. */
const ROUND_DOC_TYPE = 'Ward Round'

export default function RoundPanel() {
  const rootRef = useRef<HTMLDivElement | null>(null)

  // The clock is read once per mount so every derived value on the screen
  // (the overnight window, the day count) agrees with itself. Refreshing the
  // ward data re-reads it.
  const [now, setNow] = useState(() => new Date())
  const user = useMemo(() => getUser(), [])
  const userId = user?.id ?? 'anonymous'
  const dateKey = useMemo(() => todayKey(now), [now])

  // ── Roster ──
  const [rosterPhase, setRosterPhase] = useState<LoadPhase>('loading')
  const [rosterError, setRosterError] = useState<string | null>(null)
  const [cards, setCards] = useState<NursePatientCard[]>([])
  const [vigil, setVigil] = useState<Patient[]>([])
  const [insights, setInsights] = useState<SageInsight[]>([])
  const [boardFailed, setBoardFailed] = useState<string | null>(null)
  const [vigilFailed, setVigilFailed] = useState<string | null>(null)
  const [insightsFailed, setInsightsFailed] = useState(false)
  const [department, setDepartment] = useState<string | null>(null)
  const [asOf, setAsOf] = useState<string | null>(null)

  // ── Round progress ──
  const [statuses, setStatuses] = useState<Record<string, RoundStatus>>({})
  const [cursor, setCursor] = useState<string | null>(null)
  const [resumed, setResumed] = useState(false)
  const [showHandoff, setShowHandoff] = useState(false)
  const hydratedRef = useRef(false)

  // ── Per-patient story ──
  const [stories, setStories] = useState<Record<string, StoryEntry>>({})
  const storiesRef = useRef<Record<string, StoryEntry>>({})
  storiesRef.current = stories
  const storyInflightRef = useRef<Set<string>>(new Set())

  // ── Sage's reasoned assessment (Tier 3) ──
  // Read per bed, PREPARED one bed ahead. That split is the whole reason this
  // tier lives on the round at all: generation is ~46 s, which is unusable at a
  // deteriorating bedside and free here, because the round asks for the next
  // patient while the doctor is still standing at this one.
  const [assessments, setAssessments] = useState<Record<string, IpassReasonedAssessment | null>>({})
  const assessmentsRef = useRef<Record<string, IpassReasonedAssessment | null>>({})
  assessmentsRef.current = assessments
  const assessmentInflightRef = useRef<Set<string>>(new Set())
  /** Patients this round has already asked Sage about. One ask per patient per
   *  round: a second tap must not spend another 46 s of the single background
   *  slot on a patient who is already queued. */
  const preparedRef = useRef<Set<string>>(new Set())

  // ── Dictation ──
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const draftsRef = useRef<Record<string, string>>({})
  draftsRef.current = drafts
  const [saveStates, setSaveStates] = useState<Record<string, SaveState>>({})
  const [saveErrors, setSaveErrors] = useState<Record<string, string | null>>({})
  /** Patients whose plan this round actually wrote, so "no plan documented
   *  today" clears immediately instead of waiting for the story feed. */
  const [planSaved, setPlanSaved] = useState<Record<string, boolean>>({})
  /** Set when "mark seen" is blocked by an unsaved plan. */
  const [seenBlocked, setSeenBlocked] = useState<string | null>(null)

  const queue = useMemo(() => mergeRoster(cards, vigil), [cards, vigil])
  const cardById = useMemo(() => {
    const m = new Map<string, NursePatientCard>()
    for (const c of cards) m.set(c.patient_id, c)
    return m
  }, [cards])
  const progress = useMemo(() => progressOf(queue, statuses), [queue, statuses])

  // ── Load ──

  /* `quiet` is the refresh after a patient is admitted elsewhere in the shell.
     It shows no loading screen, which would unmount the round and any
     dictation in progress, and a request that fails keeps what is on screen. */
  const loadRoster = useCallback(async (quiet = false) => {
    if (!quiet) {
      setRosterPhase('loading')
      setRosterError(null)
    }
    const [boardRes, vigilRes, insightRes] = await Promise.allSettled([
      getShiftBoard(),
      getPatients(),
      getSageInsights(),
    ])

    // The two rosters fail independently, and either one alone is enough to run
    // a round. Only losing BOTH is fatal — anything less degrades with a visible
    // warning rather than emptying the ward.
    if (boardRes.status === 'fulfilled') {
      setCards(boardRes.value.patients ?? [])
      setDepartment(boardRes.value.department ?? null)
      setAsOf(boardRes.value.generated_at ?? null)
      setBoardFailed(null)
    } else if (!quiet) {
      setCards([])
      setBoardFailed((boardRes.reason as Error)?.message ?? 'Ward board request failed')
    }

    if (vigilRes.status === 'fulfilled') {
      setVigil(vigilRes.value)
      setVigilFailed(null)
    } else if (!quiet) {
      setVigil([])
      setVigilFailed((vigilRes.reason as Error)?.message ?? 'Patient board request failed')
    }

    if (insightRes.status === 'fulfilled') {
      setInsights(insightRes.value.insights ?? [])
      setInsightsFailed(false)
    } else if (!quiet) {
      setInsights([])
      setInsightsFailed(true)
    }

    if (boardRes.status === 'rejected' && vigilRes.status === 'rejected') {
      if (quiet) return
      setRosterError(
        (boardRes.reason as Error)?.message ?? 'The ward roster could not be loaded.',
      )
      setRosterPhase('error')
      return
    }
    setRosterError(null)
    setRosterPhase('ready')
  }, [])

  // A patient admitted from the shell joins the round without a page reload.
  useDataChanged(() => { void loadRoster(true) })

  const loadStory = useCallback(async (patientId: string, force = false) => {
    if (!patientId) return
    if (storyInflightRef.current.has(patientId)) return
    if (!force && storiesRef.current[patientId]?.phase === 'ready') return

    storyInflightRef.current.add(patientId)
    setStories(prev => ({
      ...prev,
      [patientId]: { phase: 'loading', events: prev[patientId]?.events ?? [], error: null },
    }))
    try {
      const events = await getPatientStory(patientId)
      setStories(prev => ({ ...prev, [patientId]: { phase: 'ready', events, error: null } }))
    } catch (e) {
      setStories(prev => ({
        ...prev,
        [patientId]: { phase: 'error', events: [], error: (e as Error).message },
      }))
    } finally {
      storyInflightRef.current.delete(patientId)
    }
  }, [])

  /** Read whatever assessment already exists. A PURE DB READ on the server —
   *  no model call — so it costs milliseconds however contended the appliance
   *  is, and it returns null far more often than not.
   *
   *  A FAILURE IS RECORDED AS "NOTHING", never surfaced. This is an optional
   *  second opinion on a screen a doctor is using to run a ward; the correct
   *  rendering of "we could not fetch it" is identical to the correct rendering
   *  of "there is not one", which is nothing at all. */
  const loadAssessment = useCallback(async (patientId: string) => {
    if (!patientId || assessmentInflightRef.current.has(patientId)) return
    // Already holding one: nothing to look for. The effect below re-runs on
    // every seen/skip mark (it watches `statuses` to find the next bed), and
    // without this a round of twenty patients would re-read the same assessment
    // once per button press. The re-check timers are not blocked by this — they
    // only ever run while this patient's entry is still null.
    if (assessmentsRef.current[patientId]) return
    assessmentInflightRef.current.add(patientId)
    try {
      const res = await getReasonedAssessment(patientId)
      setAssessments(prev => ({ ...prev, [patientId]: res.assessment }))
    } catch {
      setAssessments(prev => (patientId in prev ? prev : { ...prev, [patientId]: null }))
    } finally {
      assessmentInflightRef.current.delete(patientId)
    }
  }, [])

  /** Ask for one, and never wait. Silent on failure for the same reason the
   *  handover sheet is: this can be refused for permission (`sage.query` is a
   *  separate grant from reading the chart), for load, or because the feature
   *  is switched off — and a notice about an optional draft nobody requested is
   *  noise in the middle of a ward round. */
  const prepareAssessment = useCallback((patientId: string) => {
    if (!patientId || preparedRef.current.has(patientId)) return
    preparedRef.current.add(patientId)
    void prepareReasonedAssessment(patientId).catch(() => undefined)
  }, [])

  // Mount: erase every other round on this device, restore this one, load data.
  useEffect(() => {
    purgeForeignRoundState(userId, dateKey)
    const saved = loadRoundState(userId, dateKey)
    if (saved) {
      setStatuses(saved.statuses)
      setCursor(saved.cursor)
      setResumed(Object.keys(saved.statuses).length > 0)
    }
    hydratedRef.current = true
    void loadRoster()
  }, [userId, dateKey, loadRoster])

  // Reconcile the restored marks against the roster as it is now, and make sure
  // the cursor points at a bed that still exists.
  useEffect(() => {
    if (rosterPhase !== 'ready' || queue.length === 0) return
    setStatuses(prev => {
      const next = reconcileStatuses(queue, prev)
      return Object.keys(next).length === Object.keys(prev).length ? prev : next
    })
    setCursor(prev => {
      if (prev && queue.some(p => p.id === prev)) return prev
      return nextIncomplete(queue, statuses, null) ?? queue[0].id
    })
    // `statuses` is deliberately not a dependency: this only needs to run when
    // the roster changes, and including it would re-point the cursor on every
    // mark.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rosterPhase, queue])

  // Persist progress. Guarded on hydration so the empty initial state cannot
  // overwrite a round that is still being restored.
  useEffect(() => {
    if (!hydratedRef.current) return
    saveRoundState({ userId, dateKey, cursor, statuses })
  }, [userId, dateKey, cursor, statuses])

  // Current bed, and a head start on the next one. Prefetching the next
  // patient's story is what keeps the walk between beds off the critical path.
  useEffect(() => {
    if (!cursor) return
    void loadStory(cursor)
    const upcoming = nextIncomplete(queue, statuses, cursor)
    if (upcoming) void loadStory(upcoming)
  }, [cursor, queue, statuses, loadStory])

  // Read this bed's assessment; ask for this bed's AND the next bed's. Asking
  // for the current one matters too — otherwise the first patient of every
  // round would be the one nobody ever asked about.
  useEffect(() => {
    if (!cursor) return
    void loadAssessment(cursor)
    prepareAssessment(cursor)
    const upcoming = nextIncomplete(queue, statuses, cursor)
    if (upcoming) prepareAssessment(upcoming)
  }, [cursor, queue, statuses, loadAssessment, prepareAssessment])

  // ── Actions ──

  const goTo = useCallback((patientId: string | null) => {
    if (!patientId) return
    setSeenBlocked(null)
    setCursor(patientId)
  }, [])

  const markAndAdvance = useCallback((status: 'seen' | 'skipped') => {
    if (!cursor) return
    const draft = (draftsRef.current[cursor] ?? '').trim()
    // Marking a patient SEEN is the completion signal, so it may not happen
    // while a dictated plan is still sitting unsaved on the tablet. Skipping is
    // not blocked — a skip explicitly means "coming back".
    if (status === 'seen' && draft !== '') {
      setSeenBlocked(cursor)
      return
    }
    const next = nextIncomplete(queue, { ...statuses, [cursor]: status }, cursor)
    setStatuses(prev => ({ ...prev, [cursor]: status }))
    setSeenBlocked(null)
    // No next bed means the round has nothing left to drive to. Leaving the
    // doctor parked on the patient they just finished — with a "Seen · next
    // bed" button that now does nothing — reads as a stuck screen, and the one
    // artefact the end of a round has to produce is the hand-on list. So the
    // round ENDS somewhere visible rather than just running out.
    if (next) setCursor(next)
    else setShowHandoff(true)
  }, [cursor, queue, statuses])

  const savePlan = useCallback(async (patientId: string, thenMarkSeen = false) => {
    const text = (draftsRef.current[patientId] ?? '').trim()
    if (!text) return
    setSaveStates(s => ({ ...s, [patientId]: 'saving' }))
    setSaveErrors(s => ({ ...s, [patientId]: null }))
    try {
      // REFUSE, rather than queue.
      //
      // addClinicalNote routes through api/client.offlineMutate, which with no
      // connection puts the note in a localStorage queue and resolves as if it
      // had been filed. This panel used to label that outcome "queued", which
      // was honest about the queue but not about how long the queue lives: it
      // is deleted wholesale at sign-out (App.logout → clearOfflineQueue), and
      // a ward round ends in a sign-out — the tablet goes back on the trolley
      // for the next clinician. The note would be destroyed before the wi-fi
      // returned, silently, and the plan the next shift reads would simply not
      // exist. Refusing keeps the doctor's text in the box and tells her the
      // truth while she is still standing at the bed.
      assertOnlineForWrite('save this plan')
      await addClinicalNote(patientId, text, ROUND_DOC_TYPE)
      setSaveStates(s => ({ ...s, [patientId]: 'saved' }))
      setPlanSaved(p => ({ ...p, [patientId]: true }))
      setDrafts(d => ({ ...d, [patientId]: '' }))
      setSeenBlocked(null)
      void loadStory(patientId, true)
      if (thenMarkSeen) {
        const next = nextIncomplete(queue, { ...statuses, [patientId]: 'seen' }, patientId)
        setStatuses(prev => ({ ...prev, [patientId]: 'seen' }))
        // Completing the LAST bed has to end the round the same way whichever
        // button did it. Marking seen directly lands on the hand-on screen (see
        // markAndAdvance); if this path only stopped advancing, saving a plan on
        // the final patient would leave the doctor parked on someone they had
        // just finished, under a "Seen · next bed" button that does nothing, and
        // the hand-on list — the one artefact the end of a round must produce —
        // would never appear.
        if (next) setCursor(next)
        else setShowHandoff(true)
      }
    } catch (e) {
      setSaveStates(s => ({ ...s, [patientId]: 'error' }))
      setSaveErrors(s => ({ ...s, [patientId]: (e as Error).message }))
    }
  }, [loadStory, queue, statuses])

  const refresh = useCallback(() => {
    setNow(new Date())
    setStories({})
    // Dropped, not kept: "refresh ward data" means the screen should stop
    // showing anything assembled before the tap. `preparedRef` is deliberately
    // NOT cleared — a refresh is a request for current data, not a licence to
    // spend another 46 s of the one background slot per patient on the ward.
    setAssessments({})
    void loadRoster()
  }, [loadRoster])

  // ── Keyboard ──
  // Bound to the panel root, not to window: this panel stays mounted (hidden)
  // when the clinician switches tabs, and a window-level listener would let a
  // stray "n" typed in Messages silently advance the round.
  const onKeyDown = useCallback((e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented) return
    const target = e.target as HTMLElement | null
    if (target) {
      const tag = target.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable) return
    }
    if (!cursor) return
    switch (e.key) {
      case 'n':
      case 'N':
        e.preventDefault()
        goTo(nextIncomplete(queue, statuses, cursor))
        break
      case 'p':
      case 'P':
        e.preventDefault()
        goTo(previousInQueue(queue, cursor))
        break
      case 's':
      case 'S':
        e.preventDefault()
        markAndAdvance('seen')
        break
      case 'k':
      case 'K':
        e.preventDefault()
        markAndAdvance('skipped')
        break
      default:
        break
    }
  }, [cursor, queue, statuses, goTo, markAndAdvance])

  // Give the panel focus on mount so the shortcuts are live without a click.
  useEffect(() => {
    if (rosterPhase === 'ready') rootRef.current?.focus({ preventScroll: true })
  }, [rosterPhase])

  // ── Render ──

  const currentPatient = useMemo(
    () => queue.find(p => p.id === cursor) ?? null,
    [queue, cursor],
  )
  const currentStory = cursor ? stories[cursor] : undefined
  const currentAssessment = cursor ? assessments[cursor] ?? null : null
  const unsavedDrafts = useMemo(
    () => queue.filter(p => (drafts[p.id] ?? '').trim() !== ''),
    [queue, drafts],
  )

  const hasPlanToday = useMemo(() => {
    if (!cursor) return false
    if (planSaved[cursor]) return true
    return documentedToday(currentStory?.events ?? [], now)
  }, [cursor, planSaved, currentStory, now])

  // Quietly re-read while this bed has no assessment. Generation is 42-57 s and
  // a doctor spends ~90 s per patient, so a single read on arrival would miss
  // the draft it just asked for on most beds. NOT A SPINNER: nothing is on
  // screen until there is something to show, so this promises nothing — it just
  // pays out if the draft lands while the doctor is still at the bed. It stops
  // the moment one is carried, because `currentAssessment` then stops being
  // null and this effect's guard returns early.
  useEffect(() => {
    if (!cursor || currentAssessment) return
    const timers = ASSESSMENT_RECHECK_MS.map(ms =>
      setTimeout(() => { void loadAssessment(cursor) }, ms),
    )
    return () => timers.forEach(clearTimeout)
  }, [cursor, currentAssessment, loadAssessment])

  if (rosterPhase === 'loading') {
    return (
      <div className="orb-panel round-panel">
        <RoundLoading label="Loading the ward round…" lines={4} />
      </div>
    )
  }

  if (rosterPhase === 'error') {
    return (
      <div className="orb-panel round-panel">
        <RoundError
          title="The ward round could not be loaded"
          message={rosterError ?? 'Both the ward board and the patient board failed.'}
          onRetry={refresh}
        />
      </div>
    )
  }

  if (queue.length === 0) {
    return (
      <div className="orb-panel round-panel">
        <RoundEmpty
          title="No patients to round on"
          detail={
            department
              ? `There are no admitted patients in ${department} right now.`
              : 'There are no admitted patients in your scope right now.'
          }
        />
        <div className="round-empty-actions">
          <button type="button" className="round-btn round-btn--quiet" onClick={refresh}>
            Check again
          </button>
        </div>
      </div>
    )
  }

  return (
    <div
      className="orb-panel round-panel"
      ref={rootRef}
      tabIndex={-1}
      onKeyDown={onKeyDown}
      aria-label="Ward round"
    >
      <header className="round-head">
        <div className="round-head__title">
          <h1 className="round-head__h1">Ward round</h1>
          <p className="round-head__sub">
            {department ?? 'Your ward'}
            {asOf ? ` · ward data as of ${new Date(asOf).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` : ''}
          </p>
        </div>
        <div className="round-head__actions">
          <button type="button" className="round-btn round-btn--quiet" onClick={refresh}>
            Refresh ward data
          </button>
          <button
            type="button"
            className="round-btn round-btn--quiet"
            onClick={() => setShowHandoff(true)}
          >
            Finish and hand over
          </button>
        </div>
      </header>

      {resumed && (
        <p className="round-resumed" role="status">
          Resumed your round from earlier today: {progress.seen} already seen.
          <button
            type="button"
            className="round-btn round-btn--quiet round-btn--xs"
            onClick={() => { setStatuses({}); setResumed(false); setCursor(queue[0].id) }}
          >
            Start over
          </button>
        </p>
      )}

      {boardFailed && (
        <RoundDegraded
          message={`Ward board unavailable (${boardFailed}). Scores, medications and allergies are missing from this round.`}
          onRetry={refresh}
        />
      )}
      {vigilFailed && (
        <RoundDegraded
          message={`Patient board unavailable (${vigilFailed}). Admission day and risk level are missing from this round.`}
          onRetry={refresh}
        />
      )}

      {showHandoff ? (
        <RoundHandoff
          incomplete={handoffList(queue, statuses)}
          unsavedDrafts={unsavedDrafts}
          progress={progress}
          onResume={id => { setShowHandoff(false); goTo(id) }}
          onClose={() => setShowHandoff(false)}
        />
      ) : (
        <div className="round-body">
          <RoundQueueRail
            queue={queue}
            statuses={statuses}
            cursor={cursor}
            progress={progress}
            onSelect={goTo}
          />

          <main className="round-main">
            {currentPatient ? (
              <>
                <RoundSummaryView
                  patient={currentPatient}
                  card={cardById.get(currentPatient.id) ?? null}
                  storyPhase={currentStory?.phase ?? 'loading'}
                  story={currentStory?.events ?? []}
                  storyError={currentStory?.error ?? null}
                  onRetryStory={() => void loadStory(currentPatient.id, true)}
                  insights={insights}
                  insightsFailed={insightsFailed}
                  hasPlanToday={hasPlanToday}
                  assessment={currentAssessment}
                  now={now}
                />

                {/* KEYED BY PATIENT ON PURPOSE — this is a safety boundary, not
                    a render optimisation. Without it React keeps ONE dictation
                    instance across the whole round and its transient state
                    (a failed-transcription notice, the held audio behind
                    "Retry transcription", the transcribing lock) walks to the
                    next bed with the doctor. Retrying there would transcribe
                    the PREVIOUS patient's recording into THIS patient's plan,
                    and a slow transcript from the last bed would hold this
                    patient's microphone shut. Remounting gives every bed a
                    clean control; the in-flight request still resolves into the
                    patient it was recorded for, because it closes over that
                    patient's draft setter. */}
                <RoundDictation
                  key={currentPatient.id}
                  patientId={currentPatient.id}
                  hints={`${currentPatient.name}. ${currentPatient.diagnosis}`}
                  draft={drafts[currentPatient.id] ?? ''}
                  onDraftChange={text => {
                    setDrafts(d => ({ ...d, [currentPatient.id]: text }))
                    setSaveStates(s => ({ ...s, [currentPatient.id]: 'idle' }))
                  }}
                  onSave={() => void savePlan(currentPatient.id)}
                  saveState={saveStates[currentPatient.id] ?? 'idle'}
                  saveError={saveErrors[currentPatient.id] ?? null}
                />

                {seenBlocked === currentPatient.id && (
                  <div className="round-blocked" role="alert">
                    <p>
                      There is a dictated plan for {currentPatient.name} that has not
                      been saved. Marking the patient seen would leave it on this
                      tablet only.
                    </p>
                    <div className="round-blocked__actions">
                      <button
                        type="button"
                        className="round-btn round-btn--primary"
                        onClick={() => void savePlan(currentPatient.id, true)}
                      >
                        Save plan and mark seen
                      </button>
                      <button
                        type="button"
                        className="round-btn round-btn--quiet"
                        onClick={() => {
                          setDrafts(d => ({ ...d, [currentPatient.id]: '' }))
                          setSeenBlocked(null)
                          markAndAdvance('seen')
                        }}
                      >
                        Discard plan and mark seen
                      </button>
                    </div>
                  </div>
                )}

                <div className="round-actions">
                  <button
                    type="button"
                    className="round-btn round-btn--big round-btn--quiet"
                    onClick={() => markAndAdvance('skipped')}
                  >
                    Skip for now
                    <span className="round-btn__key" aria-hidden="true">K</span>
                  </button>
                  <button
                    type="button"
                    className="round-btn round-btn--big round-btn--primary"
                    onClick={() => markAndAdvance('seen')}
                  >
                    Seen · next bed
                    <span className="round-btn__key" aria-hidden="true">S</span>
                  </button>
                </div>

                <p className="round-keys">
                  Keyboard: <kbd>S</kbd> seen · <kbd>K</kbd> skip · <kbd>N</kbd> next · <kbd>P</kbd> previous
                </p>
              </>
            ) : progress.complete ? (
              <RoundEmpty
                title="Round complete"
                detail={`All ${progress.total} patients on this ward have been seen.`}
                reassurance={null}
              />
            ) : (
              <RoundLoading label="Opening the next bed…" />
            )}
          </main>
        </div>
      )}
    </div>
  )
}
