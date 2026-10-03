import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  getShiftBoard,
  type MedTask,
  type NursePatientCard,
  type ShiftBoard,
} from '../../../api/nurse'
import DeliveriesSheet from './DeliveriesSheet'
import HandoverSheet from './HandoverSheet'
import MedRoundSheet from './MedRoundSheet'
import News2Badge from '../../../components/News2Badge'
import ObsEntrySheet from './ObsEntrySheet'
import OutcomeCaptureSheet from './OutcomeCaptureSheet'
import { allergyBlocked } from './allergyBlock'
import { clockTime, missedSlotsCount, obsLabel, relativeMinutes, shiftDate } from './time'
import { news2Description } from '../../../lib/news2'
import { rovingTabKeyDown } from '../../../lib/tablist'
import { HELIX_RECEIVE, hasPermission } from '../../../lib/roles'
import { useOfflineSnapshot } from '../../../hooks/useOfflineSnapshot'
import { useVisiblePolling } from '../../../hooks/useVisiblePolling'
import OfflineBanner from '../../../components/OfflineBanner'
import './NursePanel.css'

/* ═══════════════════════════════════════════════════
   NURSE MODE — the ward nurse's home

   A doctor's screen is data-shaped: here is a patient, here is everything
   about them. A nurse on a shift does not work that way. They work a queue of
   tasks across a bay, under interruption, and the questions that matter are:

     "who is going off?"  →  Due now, sorted by deterioration then lateness
     "what do I owe?"     →  every obs and dose that is late or coming
     "how do I hand over?"→  I-PASS across the whole list at shift change

   So this panel is task-shaped. The default view is a flat cross-patient work
   list, not a patient roster — the roster is one tab over for when a nurse
   wants to look someone up. Every task routes into the surface that already
   owns that write: observations to /api/nurse (NEWS2 computed server-side),
   medications to the existing AdministerModal → /api/emar with its guards
   intact, handover to /api/ipass.

   ─── VISUAL CONTRACT (docs/DUAL_EXPERIENCE_VISUAL_SPEC.md §4) ───
   The chrome here is the SHARED home vocabulary, styled in ClinicianShell.css
   (eager) so the doctor home paints identically: .orb-home__*, .orb-row*,
   .orb-list*, .orb-section-header__label, .orb-count-chip, .orb-state*,
   .orb-tag*, .orb-skeleton*. Only what is genuinely nurse-shaped — the totals
   strip, the view tabs, the patient cards, the sheets — is styled in
   NursePanel.css.

   Two rules on this screen carry more weight than the rest:

     · THE SECTION LABEL NEVER TAKES A STATUS COLOUR. "OVERDUE" used to render
       in --status-danger-text every single shift, quiet or not. A colour that
       is present on a good day cannot mean anything on a bad one; that is the
       cry-wolf mechanism that trains staff to stop looking. The label is
       constant and the COUNT CHIP carries the tint (§1.7).
     · A SECTION THAT FAILED MUST NEVER RENDER AS EMPTY. "Nothing is due" and
       "we could not find out" are opposite clinical facts and must never share
       a shape (§6.1/§6.4).
   ═══════════════════════════════════════════════════ */

type View = 'tasks' | 'patients' | 'deliveries' | 'handover' | 'shift'
/** Tab order for the roving-tabindex keyboard pattern; must match the strip.
 *  Deliveries is gated on `helix.receive` rather than on this panel's role,
 *  so an account that reaches nurse mode without the grant is not offered a
 *  tab whose every request would 403 (lib/roles.ts). */
const nurseViews = (): readonly View[] =>
  hasPermission(HELIX_RECEIVE)
    ? (['tasks', 'patients', 'deliveries', 'handover', 'shift'] as const)
    : (['tasks', 'patients', 'handover', 'shift'] as const)

/** A single thing the nurse owes, flattened out of the per-patient board. */
interface Task {
  id: string
  kind: 'obs' | 'med'
  patient: NursePatientCard
  med?: MedTask
  /** Minutes until due; negative is late. Null sorts last. */
  minutesUntilDue: number | null
  overdue: boolean
  dueSoon: boolean
  title: string
  detail: string
}

const REFRESH_MS = 60_000

/** How often the staleness ladder re-evaluates. Display only — it re-reads the
 *  clock, never the server, and never changes a clinical value on screen. */
const STALE_TICK_MS = 30_000

/** Spec §6.5. Minutes since Orb last answered. */
const STALE_AFTER_MIN = 3
const VERY_STALE_AFTER_MIN = 10

function buildTasks(board: ShiftBoard | null): Task[] {
  if (!board) return []
  const tasks: Task[] = []
  for (const p of board.patients) {
    tasks.push({
      id: `obs:${p.patient_id}`,
      kind: 'obs',
      patient: p,
      minutesUntilDue: p.obs_minutes_until_due,
      overdue: p.obs_overdue,
      dueSoon: p.obs_due_soon,
      title: 'Observations',
      detail: `${p.obs_band} · minimum ${
        p.obs_interval_minutes >= 60
          ? `${Math.round(p.obs_interval_minutes / 60)}-hourly`
          : `${p.obs_interval_minutes}-minutely`
      }`,
    })
    for (const m of p.medications) {
      // PRN, continuous and unschedulable orders are real, but they are not
      // things the nurse "owes" at a time — listing them here would bury the
      // doses that genuinely are late.
      if (m.schedule_kind === 'prn' || m.schedule_kind === 'continuous' || m.schedule_kind === 'unscheduled') continue
      if (!m.overdue && !m.due_soon) continue
      tasks.push({
        id: `med:${m.medication_id}`,
        kind: 'med',
        patient: p,
        med: m,
        minutesUntilDue: m.minutes_until_due,
        overdue: m.overdue,
        dueSoon: m.due_soon,
        title: m.drug_name,
        detail: [m.dosage, m.route, m.frequency].filter(Boolean).join(' · '),
      })
    }
  }
  const rank = { critical: 0, elevated: 1, stable: 2 } as Record<string, number>
  return tasks.sort((a, b) => {
    // Deterioration first — a late paracetamol on a stable patient never
    // outranks obs on someone scoring 9.
    const la = rank[a.patient.news2?.level ?? 'stable'] ?? 3
    const lb = rank[b.patient.news2?.level ?? 'stable'] ?? 3
    if (la !== lb) return la - lb
    return (a.minutesUntilDue ?? 1e9) - (b.minutesUntilDue ?? 1e9)
  })
}

/**
 * The next thing that will fall due anywhere on the board.
 *
 * This is line 2 of the calm empty state and it is load-bearing: it turns
 * "nothing" into "nothing YET, and here is when". An empty queue with no
 * horizon is indistinguishable from a queue that failed to build, and a nurse
 * who cannot tell the difference will not trust either.
 *
 * Scheduled medications only, matching buildTasks: a PRN dose has no due time,
 * so promising one would be a fabricated clock.
 */
function nextDue(board: ShiftBoard | null):
  | { who: string; what: string; at: string | null; minutes: number }
  | null {
  if (!board) return null
  let best: { who: string; what: string; at: string | null; minutes: number } | null = null
  const consider = (candidate: { who: string; what: string; at: string | null; minutes: number }) => {
    if (candidate.minutes < 0) return
    if (!best || candidate.minutes < best.minutes) best = candidate
  }
  for (const p of board.patients) {
    const who = p.bed || p.name
    if (p.obs_minutes_until_due !== null) {
      consider({ who, what: 'observations', at: p.obs_due_at, minutes: p.obs_minutes_until_due })
    }
    for (const m of p.medications) {
      if (m.schedule_kind !== 'scheduled' && m.schedule_kind !== 'stat') continue
      if (m.minutes_until_due === null) continue
      consider({ who, what: m.drug_name, at: m.due_at, minutes: m.minutes_until_due })
    }
  }
  return best
}

/** Danger tint above zero, neutral at zero. Never green: on this screen green
 *  means "actively achieved", not "not currently bad" (§1.7). */
const chipTone = (count: number, tone: 'danger' | 'warn') =>
  count > 0 ? `orb-count-chip orb-count-chip--${tone}` : 'orb-count-chip'

export default function NursePanel() {
  const [liveBoard, setBoard] = useState<ShiftBoard | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [view, setView] = useState<View>('tasks')
  // Computed once per mount: the grant comes from the stored session, which does
  // not change while the panel is open.
  const views = useMemo(() => nurseViews(), [])

  const [obsFor, setObsFor] = useState<NursePatientCard | null>(null)
  const [roundFor, setRoundFor] = useState<{ patient: NursePatientCard; medId: number | null } | null>(null)
  const [flash, setFlash] = useState<{ message: string; tone: 'ok' | 'warn' } | null>(null)

  const load = useCallback(async (opts?: { quiet?: boolean }) => {
    if (!opts?.quiet) setLoading(true)
    try {
      const next = await getShiftBoard()
      setBoard(next)
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not load the shift board')
    } finally {
      setLoading(false)
    }
  }, [])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { void load() }, [load])

  // This board IS the nurse's round: her patient list, each patient's latest
  // NEWS2 and when obs are next due, and the whole eMAR (MedRoundSheet renders
  // from patient.medications, so caching the board caches the drug round too).
  // Losing it in a corridor dead zone is what offline mode exists to prevent —
  // and `board` below is deliberately the SNAPSHOT, not the raw state, so a
  // cached copy can never be rendered without the banner that dates it.
  const offline = useOfflineSnapshot('nurse:board', liveBoard, {
    liveFailed: !!error,
    onReconnect: () => void load({ quiet: true }),
  })
  const board = offline.data

  // Background refresh so lateness on screen stays true. Paused while a sheet
  // is open: re-rendering the board under a nurse mid-entry would be hostile,
  // and the sheets hold their own state anyway. useVisiblePolling also stands
  // down while another panel is on screen and refreshes on return, and picks
  // up a patient admitted from the rail without a page reload.
  useVisiblePolling(() => void load({ quiet: true }), REFRESH_MS, !(obsFor || roundFor))

  // ─── Staleness ladder (§6.5) ───
  // A separate, slower tick from the poll, and NOT paused while a sheet is
  // open. It exists precisely for the case the poll cannot cover: a tablet
  // asleep in a pocket, where setInterval is throttled and the board on screen
  // silently ages. It re-reads the wall clock only; no value on this screen
  // moves because of it, which is why it is safe to run under an open sheet.
  const [, setStaleTick] = useState(0)
  useEffect(() => {
    const timer = setInterval(() => setStaleTick(t => t + 1), STALE_TICK_MS)
    return () => clearInterval(timer)
  }, [])

  // Age is measured from when ORB ANSWERED (offline.savedAt, a client-side
  // stamp), not from board.generated_at, so a tablet whose clock has drifted
  // against the server cannot invent or hide staleness. The clock time
  // DISPLAYED is still generated_at, because that is the number a nurse
  // compares against her watch.
  //
  // Recomputed on EVERY render, deliberately un-memoised: it reads the wall
  // clock, and a memo keyed on the snapshot would freeze the ladder between
  // ticks — so a tablet woken after twenty minutes would paint one render of
  // "fresh" before correcting itself. The first paint is the one she reads.
  const staleMinutes = offline.source === 'live' && offline.savedAt !== null
    ? Math.floor((Date.now() - offline.savedAt) / 60_000)
    : 0

  const staleness: 'fresh' | 'stale' | 'very-stale' =
    staleMinutes >= VERY_STALE_AFTER_MIN ? 'very-stale'
      : staleMinutes >= STALE_AFTER_MIN ? 'stale'
        : 'fresh'

  const tasks = useMemo(() => buildTasks(board), [board])
  const overdueTasks = tasks.filter(t => t.overdue)
  const soonTasks = tasks.filter(t => !t.overdue && t.dueSoon)
  const queuedTasks = overdueTasks.length + soonTasks.length

  // ─── Skeleton geometry memory (§6.1) ───
  // The number of rows the queue rendered last time, so the second load
  // skeletons exactly the right shape and nothing shifts when data lands. In
  // memory only — no storage, no PHI.
  const [skeletonRows, setSkeletonRows] = useState(3)
  useEffect(() => {
    const rendered = overdueTasks.length + soonTasks.length
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    if (rendered > 0) setSkeletonRows(Math.min(rendered, 8))
  }, [overdueTasks.length, soonTasks.length])

  // ─── The one live region on this home (§9.5) ───
  // The totals strip used to be role="status", which re-announced the entire
  // ward summary once a minute, forever. A summary is not a change. This speaks
  // only when the overdue count GOES UP, and says nothing on a poll that
  // changed nothing or improved.
  const [announcement, setAnnouncement] = useState('')
  const lastOverdue = useRef<number | null>(null)
  useEffect(() => {
    if (!board) return
    const now = overdueTasks.length
    const previous = lastOverdue.current
    lastOverdue.current = now
    if (previous !== null && now > previous) {
      setAnnouncement(`${now} ${now === 1 ? 'task is' : 'tasks are'} now overdue.`)
    }
  }, [board, overdueTasks.length])

  const openTask = (task: Task) => {
    if (task.kind === 'obs') setObsFor(task.patient)
    else setRoundFor({ patient: task.patient, medId: task.med?.medication_id ?? null })
  }

  // Stays until dismissed or replaced by the next one. It used to clear itself
  // after six seconds, often before a nurse on a phone had seen it, and the obs
  // confirmation carries the escalation response, which is an instruction.
  const showFlash = (message: string, tone: 'ok' | 'warn' = 'ok') => {
    setFlash({ message, tone })
  }

  const lateness = (task: Task) => relativeMinutes(task.minutesUntilDue) || 'Not recorded'

  const renderTaskRow = (task: Task) => {
    const identity = `${task.patient.bed ? `${task.patient.bed} · ` : ''}${task.patient.name}`
    // The row and the badge inside it say the SAME sentence about the score —
    // two spellings of one clinical fact on one screen is how a reader learns
    // to distrust both. The trailing `›` is aria-hidden, so the destination is
    // named here or nowhere (§9.4).
    const simulated = task.patient.vitals_source === 'simulated'
    const label = [
      task.patient.name,
      task.patient.bed ? `bed ${task.patient.bed}` : null,
      news2Description(task.patient.news2),
      simulated ? 'scored from simulated vitals' : null,
      `${task.title}${task.detail ? `: ${task.detail}` : ''}`,
      lateness(task),
      task.med?.missed_slots ? `${missedSlotsCount(task.med.missed_slots)} not charted` : null,
      task.med && allergyBlocked(task.med) ? 'allergy block, needs prescriber authorisation' : null,
      task.kind === 'obs' ? 'record observations' : 'open the medication round',
    ].filter(Boolean).join(', ')

    return (
      <li key={task.id}>
        <button
          type="button"
          className={`orb-row task-row ${task.overdue ? 'orb-row--overdue' : task.dueSoon ? 'orb-row--soon' : ''}`}
          onClick={() => openTask(task)}
          aria-label={label}
        >
          <span className="orb-row__lead">
            <News2Badge news2={task.patient.news2} labelled={false} />
          </span>
          <span className="orb-row__body">
            {/* Bed first: at the moment of reading this she is looking for a
                bay, not a name. */}
            <span className="orb-row__primary">{identity}</span>
            <span className="orb-row__secondary">
              <span className={`orb-tag orb-tag--${task.kind}`} aria-hidden="true">
                {task.kind === 'obs' ? 'Obs' : 'Med'}
              </span>
              {task.title}{task.detail ? ` · ${task.detail}` : ''}
            </span>
          </span>
          <span className="orb-row__trailing">
            <span className="orb-row__value">{lateness(task)}</span>
            {task.med?.missed_slots ? (
              <span className="orb-row__note">{missedSlotsCount(task.med.missed_slots)} not charted</span>
            ) : null}
            {task.med && allergyBlocked(task.med) ? (
              <span className="orb-row__note">Allergy block</span>
            ) : null}
            {/* Honesty label: the score beside this row came from the
                simulator, not from this patient. */}
            {simulated ? <span className="orb-row__note">Simulated vitals</span> : null}
          </span>
        </button>
      </li>
    )
  }

  const renderTaskSection = (
    id: string,
    label: string,
    items: Task[],
    tone: 'danger' | 'warn',
  ) => (
    <div className="nurse-block">
      {/* The LABEL is --text-muted, always. Only the chip is allowed the tint. */}
      <h2 className="orb-section-header__label" id={`nurse-h-${id}`}>
        {label}
        <span className={chipTone(items.length, tone)}>
          {items.length}
          <span className="sr-only"> {items.length === 1 ? 'task' : 'tasks'}</span>
        </span>
      </h2>
      <ul className="orb-list orb-list--cards" aria-labelledby={`nurse-h-${id}`}>
        {items.map(renderTaskRow)}
      </ul>
    </div>
  )

  // ─── Absence, not achievement (§6.3) ───
  // Dashed, and it names the scope out loud. An empty ward is far more often a
  // scope problem than a genuinely empty ward, and "no patients" rendered as a
  // reassurance would be a nurse being told everything is fine about a list
  // she cannot see.
  const renderNoPatients = () => (
    <div className="orb-state orb-state--empty">
      <span className="orb-state__icon" aria-hidden="true">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="9" />
          <path d="M8 12h8" />
        </svg>
      </span>
      <div className="orb-state__content">
        <p className="orb-state__title">No admitted patients in this scope.</p>
        <p className="orb-state__detail">
          Your ward scope is {board?.department || 'not set'}. If you expect patients here,
          this may be a scope problem, not an empty ward.
        </p>
        {/* When it was checked, rather than "Nothing failed to load": a time is
            evidence the read happened, and a double negative is not. */}
        <p className="orb-state__reassure">
          Checked at <span className="orb-home__clock">{clockTime(board?.generated_at)}</span>.
        </p>
      </div>
    </div>
  )

  // ─── The calm empty state (§6.2) ───
  // For a nurse, "nothing due" is GOOD NEWS. It gets a solid border on a real
  // surface, not the dashed box that also means "we found nothing", and it is
  // the only place green appears on this screen. No exclamation mark, no emoji,
  // no congratulation: at 03:00 she does not want to be congratulated by
  // software, she wants to know the screen is telling the truth.
  const renderNothingDue = () => {
    const next = nextDue(board)
    const detail = next
      ? next.at
        ? <>Next {next.what} for {next.who} at <span className="orb-home__clock">{clockTime(next.at)}</span>.</>
        : <>Next {next.what} for {next.who} {relativeMinutes(next.minutes)}.</>
      : <>No observations or doses are scheduled in the next four hours.</>
    return (
      <div className="orb-state orb-state--empty orb-state--calm">
        <span className="orb-state__icon" aria-hidden="true">
          <svg width="100%" height="100%" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M4 12.5 9.5 18 20 7" />
          </svg>
        </span>
        <div className="orb-state__content">
          <p className="orb-state__title">Nothing due right now</p>
          <p className="orb-state__detail">{detail}</p>
          <p className="orb-state__reassure">
            {offline.source === 'live' ? 'Live view' : 'Saved copy'} of{' '}
            {board?.department || 'your ward'}: {board?.totals.patients ?? 0} patients, updated{' '}
            <span className="orb-home__clock">{clockTime(board?.generated_at)}</span>.
          </p>
        </div>
      </div>
    )
  }

  // ─── Loading (§6.1) ───
  // Skeletons whose geometry EQUALS the loaded row's, never a spinner and never
  // a full-page "Loading…". A background poll never reaches here: skeletons are
  // `loading && !board` only, so a 60s refresh updates in place.
  const renderSkeletons = () => (
    <div className="nurse-block" role="status" aria-busy="true" aria-live="polite">
      <span className="sr-only">Loading your shift</span>
      <ul className="orb-list orb-list--cards" aria-hidden="true">
        {Array.from({ length: skeletonRows }, (_, i) => (
          <li key={i}>
            <div className="orb-row nurse-skel">
              <span className="orb-row__lead"><span className="nurse-skel__lead" /></span>
              <span className="orb-row__body">
                <span className="orb-skeleton__bar nurse-skel__bar--a" />
                <span className="orb-skeleton__bar nurse-skel__bar--b" />
              </span>
              <span className="orb-row__trailing">
                <span className="orb-skeleton__bar nurse-skel__bar--c" />
              </span>
            </div>
          </li>
        ))}
      </ul>
    </div>
  )

  const contextClass =
    staleness === 'very-stale' ? 'orb-home__context orb-home__context--very-stale'
      : staleness === 'stale' ? 'orb-home__context orb-home__context--stale'
        : 'orb-home__context'

  return (
    <div className="orb-panel nurse-panel animate-fade-in">
      <header className="orb-home__header">
        <div>
          <h1 className="orb-home__title">My shift</h1>
          <p
            className={contextClass}
            role={staleness === 'very-stale' ? 'alert' : staleness === 'stale' ? 'status' : undefined}
          >
            {board?.department ? board.department : 'All departments'}
            {' · '}{shiftDate(board?.generated_at)}
            {board ? (
              <>
                {' · '}updated <span className="orb-home__clock">{clockTime(board.generated_at)}</span>
                {/* No banner for a four-minute-old board — that is crying wolf.
                    The clock time never disappears, because it is the number a
                    nurse checks against her own watch. */}
                {staleness !== 'fresh' && <> · {staleMinutes} min ago</>}
                {staleness === 'very-stale' && (
                  <>
                    {' '}
                    <button
                      type="button"
                      className="orb-home__refresh nurse-inline-retry"
                      onClick={() => void load()}
                      disabled={loading}
                    >
                      Retry
                    </button>
                  </>
                )}
              </>
            ) : null}
          </p>
        </div>

        {/* No "All departments" control, by design: the ward this board shows is
            decided server-side from the signed-in user (permissions.department_scope),
            the same way the Vigil patient board decides it. A nurse cannot widen
            their own PHI scope from the screen. */}
        <button
          type="button"
          className="orb-home__refresh"
          onClick={() => void load()}
          disabled={loading}
          aria-busy={loading || undefined}
        >
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
      </header>

      {/* Announces transitions, never summaries (§9.5). */}
      <p className="sr-only" role="status" aria-live="polite">{announcement}</p>

      {board && (
        <div className="nurse-totals">
          <span className="nurse-totals__lead">{board.totals.patients} patients</span>
          <span className={chipTone(board.totals.critical, 'danger')}>
            <span className="nurse-total__value">{board.totals.critical}</span>
            <span className="nurse-total__label">critical</span>
          </span>
          <span className={chipTone(board.totals.elevated, 'warn')}>
            <span className="nurse-total__value">{board.totals.elevated}</span>
            <span className="nurse-total__label">elevated</span>
          </span>
          <span className={chipTone(board.totals.obs_overdue, 'danger')}>
            <span className="nurse-total__value">{board.totals.obs_overdue}</span>
            {/* A screen reader hears the whole unit, not the ward abbreviation. */}
            <span className="nurse-total__label" aria-hidden="true">obs overdue</span>
            <span className="sr-only">observations overdue</span>
          </span>
          <span className={chipTone(board.totals.meds_overdue, 'warn')}>
            <span className="nurse-total__value">{board.totals.meds_overdue}</span>
            <span className="nurse-total__label">
              {board.totals.meds_overdue === 1 ? 'dose' : 'doses'} overdue
            </span>
          </span>
          {/* Only when there is one: a standing "0 blocked" chip would be one
              more number to learn to skip. */}
          {(board.totals.meds_allergy_blocked ?? 0) > 0 && (
            <span className={chipTone(board.totals.meds_allergy_blocked ?? 0, 'danger')}>
              <span className="nurse-total__value">{board.totals.meds_allergy_blocked}</span>
              <span className="nurse-total__label">blocked by allergy</span>
            </span>
          )}
        </div>
      )}

      {/* role="tablist" is a promise that the arrow keys work and that the strip
          holds ONE tab stop. Both halves were missing: a nurse on a screen
          reader heard "tab list", pressed the arrows and nothing moved, while
          sighted keyboard users tabbed through all three. Same handler as the
          pillar rail in ClinicianShell (lib/tablist). */}
      <nav
        className="nurse-views"
        role="tablist"
        aria-label="Nurse views"
        onKeyDown={rovingTabKeyDown(views, view, setView, id => `nurse-tab-${id}`)}
      >
        {([
          // Counts every row the tab holds. It counted only the overdue ones, so
          // "Due now (1)" sat over four rows; the overdue chip inside says that.
          ['tasks', `Due now${queuedTasks ? ` (${queuedTasks})` : ''}`],
          ['patients', `My patients${board ? ` (${board.totals.patients})` : ''}`],
          ['deliveries', 'Deliveries'],
          ['handover', 'Handover'],
          ['shift', 'End of shift'],
        ] as [View, string][]).filter(([id]) => views.includes(id)).map(([id, label]) => (
          <button
            key={id}
            id={`nurse-tab-${id}`}
            type="button"
            role="tab"
            aria-selected={view === id}
            aria-controls={`nurse-panel-${id}`}
            tabIndex={view === id ? 0 : -1}
            className={`nurse-view-tab ${view === id ? 'nurse-view-tab--active' : ''}`}
            onClick={() => setView(id)}
          >
            {label}
          </button>
        ))}
      </nav>

      <OfflineBanner snapshot={offline} what="shift board" />

      {/* Pinned above the Sage orb, not in the page flow: in the flow it sat at
          the top of the panel, which on a phone is off-screen by the time a
          sheet closes. Portalled so no ancestor can capture the fixed position. */}
      {flash && createPortal(
        <div className={`nurse-note nurse-note--${flash.tone} nurse-toast`} role="status">
          <span className="nurse-toast__message">{flash.message}</span>
          <button
            type="button"
            className="nurse-btn nurse-btn--ghost nurse-btn--compact nurse-toast__dismiss"
            onClick={() => setFlash(null)}
          >
            Dismiss
          </button>
        </div>,
        document.body,
      )}

      {/* Suppressed while the device has no network: the banner above already
          says so in words a nurse can act on, and a raw "Failed to fetch"
          underneath it only adds noise. A failure WITH a connection (a 403, a
          500) is a different problem and still shows — as a state block with
          the sentence that matters, never as a bare toast that disappears
          before it is believed (§6.4). */}
      {error && offline.online && (
        <div className="orb-state orb-state--error nurse-error" role="alert">
          <span className="orb-state__icon" aria-hidden="true">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
              <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
              <path d="M12 9v4" />
              <path d="M12 17h.01" />
            </svg>
          </span>
          <div className="orb-state__content">
            <p className="orb-state__title">Couldn't load your shift board</p>
            <p className="orb-state__detail">{error}</p>
            <p className="orb-state__warn">
              This is a display failure, not an all-clear. Do not read the missing
              information as normal. Check the chart directly.
            </p>
            <div>
              <button type="button" className="orb-home__refresh" onClick={() => void load()} disabled={loading}>
                {loading ? 'Retrying…' : 'Retry'}
              </button>
            </div>
          </div>
        </div>
      )}

      {loading && !board && renderSkeletons()}

      {board && view === 'tasks' && (
        <div className="nurse-section" id="nurse-panel-tasks" role="tabpanel" aria-labelledby="nurse-tab-tasks">
          {/* "Nothing due" is a claim about patients who ARE covered. With no
              patients on the board at all it would be a reassurance about a
              list she cannot see, so the empty queue inherits the scope
              state instead. */}
          {overdueTasks.length === 0 && soonTasks.length === 0 && (
            board.patients.length === 0 ? renderNoPatients() : renderNothingDue()
          )}
          {overdueTasks.length > 0 && renderTaskSection('overdue', 'Overdue', overdueTasks, 'danger')}
          {soonTasks.length > 0
            && renderTaskSection('soon', 'Due within 30 minutes', soonTasks, 'warn')}
          <p className="nurse-footnote">
            Observation intervals follow the RCP NEWS2 escalation table. Medication due times are derived
            from each order's frequency and the last charted dose. Orb holds no prescriber-authored
            administration schedule.
          </p>
        </div>
      )}

      {board && view === 'patients' && (
        <div className="nurse-section" id="nurse-panel-patients" role="tabpanel" aria-labelledby="nurse-tab-patients">
          {board.patients.length === 0 && renderNoPatients()}
          <ul className="patient-grid">
            {board.patients.map(p => (
              <li className={`patient-card patient-card--${p.news2?.level ?? 'none'}`} key={p.patient_id}>
                <div className="patient-card__head">
                  <News2Badge news2={p.news2} />
                  <div className="patient-card__identity">
                    <span className="patient-card__name">{p.name}</span>
                    <span className="patient-card__meta">
                      {[p.bed && `Bed ${p.bed}`, p.age && `${p.age}${p.sex ? p.sex[0].toUpperCase() : ''}`, p.mrn]
                        .filter(Boolean)
                        .join(' · ')}
                    </span>
                  </div>
                </div>

                {p.diagnosis && <p className="patient-card__dx">{p.diagnosis}</p>}

                {p.allergies.length > 0 ? (
                  <p className="patient-card__allergies">
                    Allergies: {p.allergies.map(a => a.substance).join(', ')}
                  </p>
                ) : (
                  /* Never blank and never "NKDA": an empty list is also what a
                     patient nobody asked looks like. */
                  <p className="patient-card__allergies patient-card__allergies--none">
                    No allergies recorded. Confirm with the patient.
                  </p>
                )}

                {p.news2 && p.news2.level !== 'stable' && (
                  <p className="patient-card__response">{p.obs_response}</p>
                )}

                <ul className="patient-card__facts">
                  {p.vitals_source === 'simulated' && (
                    // Honesty label: the NEWS2 above is scored from simulated
                    // readings, and they do not count as obs taken.
                    <li>Simulated vitals: the score above is not from this patient</li>
                  )}
                  <li className={p.obs_overdue ? 'is-late' : ''}>
                    {obsLabel(p.last_obs_at, p.obs_minutes_until_due)}
                  </li>
                  {/* NAME THE NOUN. This line is about DOSES and sat directly
                      under the observations line with no noun of its own, so
                      "2 overdue" read as two overdue observations — while the
                      header chip two inches above said "0 obs overdue". The
                      figures were right and the sentence was not. */}
                  <li className={p.meds_overdue ? 'is-late' : ''}>
                    {p.medications.length === 0
                      ? 'No medications charted'
                      : `Doses: ${p.medications.length} charted · ${p.meds_overdue} overdue · ${p.meds_due_soon} due soon`}
                  </li>
                  {(p.meds_allergy_blocked ?? 0) > 0 && (
                    <li className="is-late">
                      Allergy block on {p.meds_allergy_blocked} dose{p.meds_allergy_blocked === 1 ? '' : 's'}:
                      needs prescriber authorisation
                    </li>
                  )}
                </ul>

                <div className="patient-card__actions">
                  <button type="button" className="nurse-btn nurse-btn--primary" onClick={() => setObsFor(p)}>
                    Record obs
                  </button>
                  <button
                    type="button"
                    className="nurse-btn nurse-btn--secondary"
                    onClick={() => setRoundFor({ patient: p, medId: null })}
                    disabled={p.medications.length === 0}
                  >
                    Med round
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* NOT gated on `board`, for the same reason as end-of-shift below: a
          failed shift board must not hide medication that is physically sitting
          on the ward waiting to be signed for. The deliveries endpoint is ward-
          scoped on the server and needs nothing from the board. */}
      {view === 'deliveries' && views.includes('deliveries') && (
        <div className="nurse-section" id="nurse-panel-deliveries" role="tabpanel" aria-labelledby="nurse-tab-deliveries">
          <DeliveriesSheet />
        </div>
      )}

      {board && view === 'handover' && (
        <div className="nurse-section" id="nurse-panel-handover" role="tabpanel" aria-labelledby="nurse-tab-handover">
          <HandoverSheet patients={board.patients} />
        </div>
      )}

      {/* NOT gated on `board`, unlike the views above: end-of-shift capture is
          the pilot's evidence instrument, and a broken shift board must not
          stop the charge nurse recording an outcome or confirming a quiet
          shift — the board only feeds the patient picker a datalist. */}
      {view === 'shift' && (
        <div className="nurse-section" id="nurse-panel-shift" role="tabpanel" aria-labelledby="nurse-tab-shift">
          <OutcomeCaptureSheet patients={board?.patients ?? []} />
        </div>
      )}

      {obsFor && (
        <ObsEntrySheet
          patient={obsFor}
          offline={offline}
          onClose={() => setObsFor(null)}
          onRecorded={(news2, schedule) => {
            const name = obsFor.name
            setObsFor(null)
            showFlash(
              `Observations recorded for ${name}. ${news2Description(news2)}. ${schedule.response}`,
            )
            void load({ quiet: true })
          }}
        />
      )}

      {roundFor && (
        <MedRoundSheet
          patient={roundFor.patient}
          focusMedicationId={roundFor.medId}
          offline={offline}
          onClose={() => { setRoundFor(null); void load({ quiet: true }) }}
          onAdministered={record => {
            // Not given is recorded, not achieved: it never wears the green.
            if (record.status === 'given') showFlash(`${record.drug_name || 'Dose'} charted as given.`)
            else showFlash(`${record.drug_name || 'Dose'} charted as not given.`, 'warn')
          }}
        />
      )}
    </div>
  )
}
