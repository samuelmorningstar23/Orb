import {
  lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'
import { getShiftBoard, type NursePatientCard, type ShiftBoard } from '../../api/nurse'
import { getAlerts, getScribeNotes, getUnseenCounts, getUser } from '../../api/client'
import { getShiftStatus, type ShiftStatus, type ShiftWindow } from '../../api/shadowOutcome'
import News2Badge from '../../components/News2Badge'
import OfflineBanner from '../../components/OfflineBanner'
import { news2Description } from '../../lib/news2'
import { askSage, gotoTab, openPatient } from '../../lib/orbBus'
import { hasPermission, OUTCOME_RECORD } from '../../lib/roles'
import { formatClinicalDayShort, formatClinicalTime, formatUTCTime, parseUTC } from '../../lib/datetime'
import {
  loadRoundState, mergeRoster, progressOf, reconcileStatuses, todayKey,
} from '../../components/round/roundQueue'
import { useFocusTrap } from '../../hooks/useFocusTrap'
import { useOfflineSnapshot } from '../../hooks/useOfflineSnapshot'
import { useVisiblePolling } from '../../hooks/useVisiblePolling'
import './DoctorHomePanel.css'

/* ═══════════════════════════════════════════════════════════════════════════
   DOCTOR HOME — "Today"

   A doctor's first minute is a COMPOSITION, not a panel. Who is deteriorating
   since I last looked, what is waiting on my signature or acknowledgement, what
   came back, and what do I want to ask — four tabs' worth of question, answered
   simultaneously (docs/DUAL_EXPERIENCE_DESIGN.md §1.3.1, §2.2).

   THIS SCREEN READS AND NAVIGATES. NOTHING ELSE.
   Every block composes an endpoint that already exists, and the only thing a
   press can do is move the clinician to the panel that owns the write. That is
   what makes a brand-new landing screen safe to put in front of real doctors
   inside the pilot window: it adds no server surface, no write path, and no way
   to change a patient record that did not already exist behind an audited,
   permission-gated panel. DoctorHomePanel.test.tsx holds that as an invariant —
   every request this panel issues is a GET.

   Two rules govern how it LOOKS, and both are load-bearing rather than
   decorative (docs/DUAL_EXPERIENCE_VISUAL_SPEC.md §3.3, §6):

     ON A QUIET WARD THERE IS NO SATURATED COLOUR ON THIS SCREEN AT ALL. The
     escalation is structural — first block, top of the reading column, the only
     two-line bordered objects on the page. Colour does one job on top of that:
     a left rule and a filled NEWS2 badge on the patients that earned it. Which
     is what makes those badges mean something when they do appear.

     A SECTION THAT FAILED NEVER RENDERS AS EMPTY. "There is nothing to report"
     and "we could not find out" look identical when both render as nothing, so
     every failure gets an error block that says in words that it is a display
     failure and not an all-clear.
   ═══════════════════════════════════════════════════════════════════════════ */

/* The capture sheet is code-split away from the landing screen. It is the one
   heavy thing on this page (it drags NursePanel.css with it, by its own
   design), it renders for one role-permission in a modal nobody opens on most
   visits, and this panel is the first paint after sign-in. Composed, never
   copied: the doctor and the charge nurse record into the SAME component, so
   the pilot cannot end up with two ground truths. */
const OutcomeCaptureSheet = lazy(() => import('./nurse/OutcomeCaptureSheet'))

/** Background refresh of the live board. Matches the nurse home. */
const REFRESH_MS = 60_000
/** Deteriorating rows on the home. Past this, the Vigil board is the answer. */
const DETERIORATING_LIMIT = 5
/** Skeleton rows before this panel has ever seen data (spec §6.1). */
const DEFAULT_SKELETON_ROWS = 3

/* ── Small pure helpers ───────────────────────────────────────────────────── */

const BAND_RANK: Record<string, number> = { critical: 0, elevated: 1, stable: 2 }

/** Sickest first: band, then the raw NEWS2, then bed so the order is stable.
 *  The same ordering the ward board and the nurse queue already use — a doctor
 *  and a nurse looking at the same two patients must not be shown them in two
 *  different orders. */
function byDeterioration(a: NursePatientCard, b: NursePatientCard): number {
  const ra = BAND_RANK[a.news2?.level ?? 'stable'] ?? 3
  const rb = BAND_RANK[b.news2?.level ?? 'stable'] ?? 3
  if (ra !== rb) return ra - rb
  const sa = a.news2?.raw_news2 ?? -1
  const sb = b.news2?.raw_news2 ?? -1
  if (sa !== sb) return sb - sa
  return (a.bed ?? '').localeCompare(b.bed ?? '')
}

/** "GM-04 · Anita Rao" — bed first, because a doctor is looking for a room. */
function rowTitle(p: NursePatientCard): string {
  return p.bed ? `${p.bed} · ${p.name}` : p.name
}

/** Diagnosis plus up to two abnormal vitals, in the words NEWS2 already uses.
 *  `drivers` is the server's own attribution ("Tachycardia (HR 118)"), so this
 *  cannot drift from the badge sitting next to it. */
function rowDetail(p: NursePatientCard): string {
  const drivers = (p.news2?.drivers ?? []).slice(0, 2)
  return [p.diagnosis, ...drivers].filter(Boolean).join(' · ')
}

function plural(n: number, one: string, many = `${one}s`): string {
  return n === 1 ? one : many
}

/** Tue 12 Aug — the orientation that matters when covering someone else's list. */
function dateLabel(now: Date = new Date()): string {
  return formatClinicalDayShort(now)
}

function messageOf(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback
}

/**
 * Run one read so that NOTHING it does can escape as an unhandled rejection.
 *
 * `Promise.allSettled([a(), b()])` looks like it does this and does not: the
 * array is evaluated first, so if b() throws SYNCHRONOUSLY the array literal
 * aborts, a()'s promise is never handed to allSettled, and its rejection ends
 * up orphaned — a section that renders its error state while the console fills
 * with unhandled rejections nobody can trace back here. Taking a thunk moves
 * every access, including the module lookup, inside the promise chain.
 */
function settled<T>(run: () => Promise<T>): Promise<PromiseSettledResult<T>> {
  return Promise.resolve().then(run).then(
    value => ({ status: 'fulfilled', value }) as PromiseFulfilledResult<T>,
    reason => ({ status: 'rejected', reason }) as PromiseRejectedResult,
  )
}

/** True for the one failure mode §6.6 says must never be silent: the client
 *  permission mirror drifted and the server refused a read this account was
 *  offered. Silence there is indistinguishable from a calm ward. */
function isRefusal(err: unknown): boolean {
  return typeof (err as { status?: number } | null)?.status === 'number'
    && (err as { status: number }).status === 403
}

/* ── State blocks ─────────────────────────────────────────────────────────── */

/** Skeletons whose geometry IS the loaded row's geometry — same chassis, same
 *  min-height, same lead box — so nothing shifts when the data lands. */
function SkeletonRows({ rows, lead, label }: { rows: number; lead: boolean; label: string }) {
  return (
    <div role="status" aria-busy="true">
      <span className="sr-only">{label}</span>
      <ul className="orb-list orb-list--cards" aria-hidden="true">
      {Array.from({ length: Math.max(1, rows) }, (_, i) => (
        <li key={i} className="orb-row">
          {lead && (
            <span className="orb-row__lead">
              <span className="orb-skeleton__bar today-skeleton__lead" />
            </span>
          )}
          <span className="orb-row__body orb-skeleton">
            <span className="orb-skeleton__bar today-skeleton__bar--wide" />
            <span className="orb-skeleton__bar today-skeleton__bar--narrow" />
          </span>
          <span className="orb-row__trailing">
            <span className="orb-skeleton__bar today-skeleton__bar--trailing" />
          </span>
        </li>
      ))}
      </ul>
    </div>
  )
}

const CheckIcon = (
  <svg className="orb-state__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M20 6L9 17l-5-5" />
  </svg>
)

const WarnIcon = (
  <svg className="orb-state__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
    <path d="M12 9v4" /><path d="M12 17h.01" />
  </svg>
)

const DashIcon = (
  <svg className="orb-state__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <circle cx="12" cy="12" r="9" /><path d="M8 12h8" />
  </svg>
)

/**
 * An emptiness that is an ACHIEVEMENT — solid border, one small green check.
 * Dashed says "absence"; solid says "this is the answer". The detail line names
 * what was searched and when, because "nothing here" is a claim and "no patient
 * scored above 4 as of 09:14, across 12 patients" is evidence, and the
 * difference is whether a tired reader can trust it (spec §6.2).
 */
function CalmEmpty({ title, detail, reassure }: {
  title: string; detail: string; reassure?: string
}) {
  return (
    <div className="orb-state orb-state--empty orb-state--calm">
      {CheckIcon}
      <div className="orb-state__content">
        <p className="orb-state__title">{title}</p>
        <p className="orb-state__detail">{detail}</p>
        {reassure && <p className="orb-state__reassure">{reassure}</p>}
      </div>
    </div>
  )
}

/** A plain absence: nothing was found, and that is not an achievement. */
function PlainEmpty({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="orb-state orb-state--empty">
      {DashIcon}
      <div className="orb-state__content">
        <p className="orb-state__title">{title}</p>
        <p className="orb-state__detail">{detail}</p>
      </div>
    </div>
  )
}

/**
 * THE MOST IMPORTANT STATE ON THIS SCREEN.
 *
 * Never a toast — a toast for a failed clinical read disappears before it is
 * believed. Never a bare "something went wrong": the real message is what tells
 * a clinician whether to wait or to walk to the chart. And always the same last
 * sentence, because the reader's instinct on seeing a section with nothing in
 * it is to conclude there is nothing to see.
 */
function ErrorState({ title, detail, onRetry }: {
  title: string; detail: string; onRetry?: () => void
}) {
  return (
    <div className="orb-state orb-state--error" role="alert">
      {WarnIcon}
      <div className="orb-state__content">
        <p className="orb-state__title">{title}</p>
        <p className="orb-state__detail">{detail}</p>
        <p className="orb-state__warn">
          This is a display failure, not an all-clear. Do not read the missing
          information as normal. Check the chart directly.
        </p>
        {onRetry && (
          <div>
            <button type="button" className="orb-home__refresh" onClick={onRetry}>Retry</button>
          </div>
        )}
      </div>
    </div>
  )
}

/** Micro-caps label + count chip. The LABEL never takes a status colour — it is
 *  constant, and "PENDING" in red every single shift is how a colour stops
 *  meaning anything. The chip carries the tint. */
function SectionHeader({ id, label, count, tone, unit }: {
  id: string
  label: string
  count: number
  /** Neutral at zero, always: green means "actively achieved" here, never "not
   *  currently bad", and a zero adverse count is not an achievement. */
  tone?: 'danger' | 'warn' | 'info'
  unit: string
}) {
  const chipTone = count > 0 && tone ? ` orb-count-chip--${tone}` : ''
  return (
    <h2 className="orb-section-header__label" id={id}>
      {label}
      <span className={`orb-count-chip${chipTone}`}>
        {count}
        <span className="sr-only"> {unit}</span>
      </span>
    </h2>
  )
}

function Section({ children }: { children: ReactNode }) {
  return <section className="today-section">{children}</section>
}

/* ── Panel ────────────────────────────────────────────────────────────────── */

interface RoundSummary { seen: number; total: number; remaining: number; skipped: number }

export default function DoctorHomePanel() {
  const user = getUser()
  const canRecordOutcome = hasPermission(OUTCOME_RECORD, user?.role ?? null)

  /* The ward board is the clinical signal and carries NEWS2, which is what the
     Deteriorating block is FOR. /vigil/patients — the roster the doctor's own
     patient tab loads — does not carry it; the shift board does, and a doctor
     holds both permissions it requires (patient.read_all + vitals.read,
     server/permissions.py). Ward scope is derived server-side from the session
     on both, so this is the same cohort the doctor already sees, asked a
     different way. */
  const [liveBoard, setLiveBoard] = useState<ShiftBoard | null>(null)
  const [boardError, setBoardError] = useState('')
  const [boardLoading, setBoardLoading] = useState(true)

  const [alerts, setAlerts] = useState<number | null>(null)
  const [alertsError, setAlertsError] = useState('')

  const [drafts, setDrafts] = useState<number | null>(null)
  const [draftsError, setDraftsError] = useState('')

  const [unseen, setUnseen] = useState<Record<string, number> | null>(null)
  const [unseenError, setUnseenError] = useState('')
  const [unseenAt, setUnseenAt] = useState<number | null>(null)

  const [shift, setShift] = useState<ShiftStatus | null>(null)
  const [shiftError, setShiftError] = useState('')
  const [shiftRefused, setShiftRefused] = useState(false)

  const [refreshing, setRefreshing] = useState(false)
  const [outcomeOpen, setOutcomeOpen] = useState(false)
  const [announcement, setAnnouncement] = useState('')
  const [question, setQuestion] = useState('')

  /* Skeleton counts, in memory only — no storage, no PHI. First load shows
     three; every load after that shows exactly as many rows as the section had
     last time, so the page does not jump when the data lands. Survives tab
     switches because the shell keeps visited panels mounted. */
  const [skeletonRows, setSkeletonRows] = useState({
    deteriorating: DEFAULT_SKELETON_ROWS,
    results: DEFAULT_SKELETON_ROWS,
  })

  /* ── Loads ──
     Split deliberately, and the split is about cost, not convenience.

     LIVE is the ward board and the alert list: cheap enough to poll, and stale
     deterioration is the one thing on this screen that could hurt somebody.

     ON-DEMAND is /chart/unseen, the Scribe drafts and the shift status. Each
     writes a fail-closed PHI-read audit row per call, and VigilPanel already
     records why /chart/unseen must not ride a timer: a per-tick write into
     encrypted SQLite is the exact shape behind the July event-loop stalls. So
     they run when a human asks — mount, Refresh, or coming back to this tab —
     which is the same rule the patient board settled on. */

  /* Every loader below is total: it resolves, always, and turns any failure —
     including one thrown before a request leaves the process — into the error
     state of the section it belongs to. An unhandled rejection on the app's
     landing screen is not a logging problem; it is a section that renders as
     blank because nobody caught the throw, which is the exact "silent empty"
     failure mode this page is written against. */

  const loadLive = useCallback(async (opts?: { quiet?: boolean }) => {
    if (!opts?.quiet) setBoardLoading(true)
    try {
      const [board, alertRows] = await Promise.all([
        settled(() => getShiftBoard()),
        settled(() => getAlerts()),
      ])
      if (board.status === 'fulfilled') {
        setLiveBoard(board.value)
        setBoardError('')
      } else {
        setBoardError(messageOf(board.reason, 'The ward board could not be loaded.'))
      }
      if (alertRows.status === 'fulfilled') {
        setAlerts(alertRows.value.length)
        setAlertsError('')
      } else {
        setAlerts(null)
        setAlertsError(messageOf(alertRows.reason, 'Safety alerts could not be read.'))
      }
    } catch (e) {
      setBoardError(messageOf(e, 'The ward board could not be loaded.'))
      setAlerts(null)
      setAlertsError(messageOf(e, 'Safety alerts could not be read.'))
    } finally {
      setBoardLoading(false)
    }
  }, [])

  const loadOnDemand = useCallback(async () => {
    const [counts, notes] = await Promise.all([
      settled(() => getUnseenCounts()),
      settled(() => getScribeNotes()),
    ])
    if (counts.status === 'fulfilled') {
      setUnseen(counts.value)
      setUnseenAt(Date.now())
      setUnseenError('')
    } else {
      setUnseen(null)
      setUnseenError(messageOf(counts.reason, 'New results could not be read.'))
    }
    if (notes.status === 'fulfilled') {
      /* Author-scoped by the server when no patient is named: these are the
         caller's OWN drafts. Notes drafted by somebody else and awaiting a
         doctor's signature are not reachable without naming a patient, so the
         row says whose drafts these are rather than implying a ward-wide
         signing queue it cannot see. */
      setDrafts(notes.value.filter(
        n => ((n as { status?: string } | null)?.status ?? 'draft') === 'draft').length)
      setDraftsError('')
    } else {
      setDrafts(null)
      setDraftsError(messageOf(notes.reason, 'Your Scribe drafts could not be read.'))
    }
  }, [])

  const ward = user?.department ?? ''
  const loadShift = useCallback(async (): Promise<void> => {
    if (!canRecordOutcome || !ward) return
    try {
      setShift(await getShiftStatus(ward))
      setShiftError('')
      setShiftRefused(false)
    } catch (e) {
      setShift(null)
      setShiftRefused(isRefusal(e))
      setShiftError(messageOf(e, 'The shift status could not be read.'))
    }
  }, [canRecordOutcome, ward])

  const refreshAll = useCallback(async (opts?: { quiet?: boolean }) => {
    setRefreshing(true)
    try {
      await Promise.all([
        settled(() => loadLive(opts)),
        settled(() => loadOnDemand()),
        settled(() => loadShift()),
      ])
    } finally {
      setRefreshing(false)
    }
  }, [loadLive, loadOnDemand, loadShift])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { void refreshAll() }, [refreshAll])

  /* Paused while the capture sheet is open: re-rendering the page under a
     clinician mid-entry would be hostile, and useVisiblePolling already stops
     the timer while the browser tab is in the background. */
  useVisiblePolling(() => { void loadLive({ quiet: true }) }, REFRESH_MS, !outcomeOpen)

  /* Coming BACK to Today is a user action, so it earns the on-demand reads.
     The shell keeps every visited panel mounted behind `display: none`, so
     document.visibilityState never changes on a tab switch and the poller above
     cannot see this; an observer on the root can, because a display:none
     ancestor gives the element no box. Without it a doctor who signs a note in
     Scribe returns to a Today still claiming it is unsigned. */
  const rootRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = rootRef.current
    if (!el || typeof IntersectionObserver === 'undefined') return
    let visible = true
    const io = new IntersectionObserver(entries => {
      const now = entries.some(e => e.isIntersecting)
      if (now && !visible) { void loadOnDemand(); void loadShift(); void loadLive({ quiet: true }) }
      visible = now
    })
    io.observe(el)
    return () => io.disconnect()
  }, [loadLive, loadOnDemand, loadShift])

  /* The board is passed THROUGH the snapshot, never around it, so a cached copy
     can never render without the banner that dates it. */
  const offline = useOfflineSnapshot('today:board', boardLoading || boardError ? null : liveBoard, {
    liveFailed: !!boardError,
    onReconnect: () => { void refreshAll({ quiet: true }) },
  })
  const board = offline.data

  /* ── Derived ── */

  const deteriorating = useMemo(() => {
    if (!board) return []
    return board.patients
      .filter(p => p.news2?.level === 'critical' || p.news2?.level === 'elevated')
      .sort(byDeterioration)
  }, [board])

  const shown = deteriorating.slice(0, DETERIORATING_LIMIT)

  const resultRows = useMemo(() => {
    if (!unseen) return []
    const byId = new Map((board?.patients ?? []).map(p => [p.patient_id, p]))
    return Object.entries(unseen)
      .filter(([, n]) => n > 0)
      .map(([id, n]) => ({ id, count: n, patient: byId.get(id) ?? null }))
      .sort((a, b) => b.count - a.count || a.id.localeCompare(b.id))
  }, [unseen, board])

  const unseenTotal = resultRows.reduce((sum, r) => sum + r.count, 0)

  /* Round progress is computed with the round's OWN functions over the round's
     own persisted marks — never a second idea of what "seen" means. The roster
     is this board rather than the Round panel's merge of two rosters; they are
     the same department-scoped cohort, and reconcileStatuses drops marks for
     anyone since discharged so the count cannot drift upward. */
  const round: RoundSummary | null = useMemo(() => {
    if (!board || !user?.id) return null
    const state = loadRoundState(user.id, todayKey())
    if (!state) return null
    const queue = mergeRoster(board.patients, [])
    const statuses = reconcileStatuses(queue, state.statuses)
    const p = progressOf(queue, statuses)
    if (p.total === 0 || p.complete || p.seen === 0) return null
    return { seen: p.seen, total: p.total, remaining: p.remaining, skipped: p.skipped }
  }, [board, user?.id])

  const owedShifts: ShiftWindow[] = useMemo(() => {
    if (!shift?.status_available) return []
    return shift.shifts.filter(w => w.wards.some(x => x.status === 'outstanding'))
  }, [shift])

  const pendingCount = (drafts ?? 0) + (alerts ?? 0) + (round ? 1 : 0)
  const pendingFailures = [
    draftsError && 'notes to sign',
    alertsError && 'safety alerts',
    !round && boardError && 'the ward round',
  ].filter(Boolean) as string[]

  /* Announce ONLY an adverse increase, at most once per load. A summary read
     out every poll is not a change announcement — it is noise that teaches the
     listener to tune the region out. Decreases are silent. */
  const lastUnseenRef = useRef<number | null>(null)
  useEffect(() => {
    if (unseen === null) return
    const previous = lastUnseenRef.current
    lastUnseenRef.current = unseenTotal
    if (previous !== null && unseenTotal > previous) {
      const delta = unseenTotal - previous
      setAnnouncement(`${delta} new ${plural(delta, 'result')} since you last looked.`)
    }
  }, [unseen, unseenTotal])

  /* Remember the last non-empty row counts so a reload's skeleton is the size
     of the board it is replacing. Adjusted during render rather than in an
     effect: the effect spent a whole extra render per poll to update a value
     that is only ever read while data is ABSENT. Converges immediately — once
     stored, the next render computes the same pair and stops. */
  const nextSkeleton = {
    deteriorating: shown.length || skeletonRows.deteriorating,
    results: resultRows.length || skeletonRows.results,
  }
  if (nextSkeleton.deteriorating !== skeletonRows.deteriorating
      || nextSkeleton.results !== skeletonRows.results) {
    setSkeletonRows(nextSkeleton)
  }

  /* ── Staleness ladder (spec §6.5) — three steps, no banner. A banner for a
     four-minute-old board is crying wolf; the clock time never disappears,
     because a clinician compares it against her own watch. */
  const generatedAt = board?.generated_at ?? null
  const [nowTick, setNowTick] = useState(() => Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNowTick(Date.now()), 30_000)
    return () => clearInterval(timer)
  }, [])
  const ageMinutes = useMemo(() => {
    if (!generatedAt) return null
    const then = parseUTC(generatedAt).getTime()
    if (Number.isNaN(then)) return null
    return Math.max(0, Math.floor((nowTick - then) / 60_000))
  }, [generatedAt, nowTick])
  const veryStale = ageMinutes !== null && ageMinutes > 10
  const stale = ageMinutes !== null && ageMinutes >= 3 && !veryStale

  const wardLabel = board?.department || user?.department || 'your ward'
  const patientCount = board?.totals.patients ?? 0
  const boardClock = generatedAt ? formatUTCTime(generatedAt) : ''

  /* ── Ask Sage ── */
  const askRef = useRef<HTMLTextAreaElement>(null)
  const submitQuestion = () => {
    const text = question.trim()
    if (!text) return
    /* Navigation, not a request. The question rides the bus into the Sage tab,
       which owns every server call Sage makes — this composer has never held a
       token, a transport or a write. */
    askSage(text)
    setQuestion('')
  }

  /* ── Render ── */

  const deterioratingBody = () => {
    if (boardError && !board) {
      return <ErrorState title="Couldn't load the NEWS2 board" detail={boardError}
        onRetry={() => { void loadLive() }} />
    }
    if (!board) {
      return <SkeletonRows rows={skeletonRows.deteriorating} lead label="Loading the highest NEWS2 scores" />
    }
    if (shown.length === 0) {
      return (
        <CalmEmpty
          title="No patient is scoring above threshold."
          detail={`All ${patientCount} ${plural(patientCount, 'patient')} on ${wardLabel} are at NEWS2 4 or below${boardClock ? ` as of ${boardClock}` : ''}.`}
          reassure="Nothing failed to load."
        />
      )
    }
    return (
      <ul className="orb-list orb-list--cards" aria-labelledby="today-deteriorating">
        {shown.map(p => {
          const level = p.news2?.level
          const modifier = level === 'critical' ? ' orb-row--overdue'
            : level === 'elevated' ? ' orb-row--soon' : ''
          const newCount = unseen?.[p.patient_id] ?? 0
          const detail = rowDetail(p)
          return (
            <li key={p.patient_id}>
              <button
                type="button"
                className={`orb-row orb-row--link${modifier}`}
                onClick={() => openPatient(p.patient_id, p.name)}
                aria-label={[
                  p.name,
                  p.bed ? `bed ${p.bed}` : null,
                  news2Description(p.news2),
                  newCount > 0 ? `${newCount} new ${plural(newCount, 'result')}` : null,
                ].filter(Boolean).join(', ') + '. Open patient'}
              >
                <span className="orb-row__lead">
                  <News2Badge news2={p.news2} labelled={false} />
                </span>
                <span className="orb-row__body">
                  <span className="orb-row__primary">{rowTitle(p)}</span>
                  {detail && <span className="orb-row__secondary">{detail}</span>}
                </span>
                <span className="orb-row__trailing">
                  {newCount > 0 && <span className="orb-row__value">{newCount} new</span>}
                  <span className="orb-row__go" aria-hidden="true">›</span>
                </span>
              </button>
            </li>
          )
        })}
        {/* A composed home that reproduces the roster is the roster with extra
            steps. The slice is deliberate, and this row says so. */}
        <li>
          <button
            type="button"
            className="orb-row orb-row--link today-more"
            onClick={() => gotoTab('vigil')}
            aria-label={`See all ${patientCount} ${plural(patientCount, 'patient')}. Open the patient board`}
          >
            <span className="orb-row__body">
              <span className="orb-row__primary today-more__label">
                See all {patientCount} {plural(patientCount, 'patient')}
              </span>
            </span>
            <span className="orb-row__trailing">
              <span className="orb-row__go" aria-hidden="true">›</span>
            </span>
          </button>
        </li>
      </ul>
    )
  }

  const pendingBody = () => {
    if (drafts === null && alerts === null && !draftsError && !alertsError) {
      return <SkeletonRows rows={2} lead={false} label="Loading what is waiting on you" />
    }
    const rows: ReactNode[] = []
    if (drafts) {
      rows.push(
        <li key="sign">
          <button type="button" className="orb-row orb-row--link"
            onClick={() => gotoTab('scribe')}
            aria-label={`${drafts} ${plural(drafts, 'note')} awaiting your signature: open Scribe`}>
            <span className="orb-row__body">
              <span className="orb-row__primary">
                <span className="orb-tag orb-tag--sign">Sign</span>
                {drafts} {plural(drafts, 'note')} awaiting your signature
              </span>
              <span className="orb-row__secondary">
                Your own drafts. Notes drafted by someone else are listed in Scribe.
              </span>
            </span>
            <span className="orb-row__trailing">
              <span className="orb-row__go" aria-hidden="true">›</span>
            </span>
          </button>
        </li>,
      )
    }
    if (alerts) {
      rows.push(
        <li key="ack">
          <button type="button" className="orb-row orb-row--link"
            /* DELIBERATE DEVIATION from spec §3.4, which routes ACK to Vigil.
               These are Relay's targeted safety alerts and Relay is where they
               are acknowledged (resolveAlert, RelayPanel). Sending a doctor to
               a patient roster with no control to resolve what they were just
               told about is the same defect class as a button that only 403s —
               the one this document family exists to kill. */
            onClick={() => gotoTab('relay')}
            aria-label={`${alerts} safety ${plural(alerts, 'alert')} unresolved: open Messages`}>
            <span className="orb-row__body">
              <span className="orb-row__primary">
                <span className="orb-tag orb-tag--ack">Ack</span>
                {alerts} safety {plural(alerts, 'alert')} unresolved
              </span>
              <span className="orb-row__secondary">Acknowledge or escalate in Messages.</span>
            </span>
            <span className="orb-row__trailing">
              <span className="orb-row__go" aria-hidden="true">›</span>
            </span>
          </button>
        </li>,
      )
    }
    if (round) {
      rows.push(
        <li key="round">
          <button type="button" className="orb-row orb-row--link"
            onClick={() => gotoTab('round')}
            aria-label={`Ward round: ${round.seen} of ${round.total} patients seen, ${round.remaining} remaining: resume the round`}>
            <span className="orb-row__body">
              {/* No tag here, deliberately: OBS/MED/SIGN/ACK are categories of
                  work, and the round is a single named activity that the
                  sentence already names. */}
              <span className="orb-row__primary">
                Ward round: {round.seen} of {round.total} patients seen
              </span>
              {/* No progress bar: it duplicates the number beside it and would
                  be the only animated geometry on the page. */}
              <span className="orb-row__secondary">
                {round.remaining} still to see{round.skipped ? ` · ${round.skipped} skipped` : ''}
              </span>
            </span>
            <span className="orb-row__trailing">
              <span className="orb-row__value today-resume">Resume</span>
              <span className="orb-row__go" aria-hidden="true">›</span>
            </span>
          </button>
        </li>,
      )
    }

    const failure = pendingFailures.length > 0 ? (
      <ErrorState
        key="error"
        title={`Couldn't check ${pendingFailures.join(' or ')}`}
        detail={[draftsError, alertsError, !round && boardError ? boardError : '']
          .filter(Boolean).join(' · ')}
        onRetry={() => { void loadOnDemand(); void loadLive() }}
      />
    ) : null

    if (rows.length === 0) {
      return (
        <>
          {!failure && (
            <CalmEmpty
              title="Nothing is waiting on you."
              /* Naming the three things that were checked and found empty is
                 what makes the emptiness a statement rather than a shrug. */
              detail={`0 notes to sign · 0 unresolved safety alerts · ${
                round === null && !boardError ? 'no ward round in progress' : 'ward round status unavailable'
              }.`}
            />
          )}
          {failure}
        </>
      )
    }
    return (
      <>
        <ul className="orb-list orb-list--rows" aria-labelledby="today-pending">{rows}</ul>
        {failure}
      </>
    )
  }

  const resultsBody = () => {
    if (!offline.online) {
      return (
        <PlainEmpty
          title="New results are not available offline."
          detail="This count is read live and is not cached, so no number is shown rather than an old one. It will return when the connection does."
        />
      )
    }
    if (unseenError) {
      return <ErrorState title="Couldn't load new results" detail={unseenError}
        onRetry={() => { void loadOnDemand() }} />
    }
    if (!unseen) {
      return <SkeletonRows rows={skeletonRows.results} lead={false} label="Loading new results" />
    }
    if (resultRows.length === 0) {
      return (
        <CalmEmpty
          title="No new results since you last looked."
          detail={`Last checked ${unseenAt ? formatClinicalTime(unseenAt) : '—'}.`}
          reassure="Nothing failed to load."
        />
      )
    }
    /* A fact list, not an alarm: divided rows, no left rules, no status colour,
       counts in the neutral trailing token. It is third in the hierarchy of
       attention and it is treated that way. */
    return (
      <ul className="orb-list orb-list--rows" aria-labelledby="today-results">
        {resultRows.map(r => {
          const name = r.patient?.name ?? r.id
          const bed = r.patient?.bed
          return (
            <li key={r.id}>
              <button type="button" className="orb-row orb-row--link"
                onClick={() => openPatient(r.id, r.patient?.name)}
                aria-label={`${name}${bed ? `, bed ${bed}` : ''}, ${r.count} new ${plural(r.count, 'result')}. Open patient`}>
                <span className="orb-row__body">
                  <span className="orb-row__primary">{bed ? `${bed} ${name}` : name}</span>
                </span>
                <span className="orb-row__trailing">
                  <span className="orb-row__value">{r.count}</span>
                  <span className="orb-row__go" aria-hidden="true">›</span>
                </span>
              </button>
            </li>
          )
        })}
      </ul>
    )
  }

  const shiftBody = () => {
    if (!offline.online) {
      return (
        <PlainEmpty
          title="End-of-shift capture needs a connection."
          detail="Confirmations are never queued offline: one the recorder believes was saved but was not turns an event-free shift into uncovered time."
        />
      )
    }
    /* A 403 arriving here means the client permission mirror drifted from the
       server. §6.6: that is an error, never silence — a silently empty section
       after a refusal is indistinguishable from a ward with nothing owed. */
    if (shiftRefused) {
      return <ErrorState title="Orb refused this read for your account"
        detail={shiftError} onRetry={() => { void loadShift() }} />
    }
    const owed = owedShifts.length
    const first = owedShifts[0]
    const primary = owed === 0
      ? 'End-of-shift outcomes'
      : owed === 1 && first
        ? `Confirm the ${first.shift} shift (${first.label})`
        : `Confirm ${owed} shifts`
    const secondary = !ward
      ? 'No ward is recorded on your account, so nothing could be checked. Name the ward inside.'
      : shiftError || !shift
        ? 'Couldn’t check which shifts this ward has confirmed. Open to see.'
        : !shift.status_available
          ? 'The confirmation history could not be read. Recording still works.'
          : owed === 0
            ? 'Both recent shifts are recorded.'
            : owed === 1
              ? 'No event or confirmation was recorded for this shift.'
              : 'No event or confirmation was recorded for these shifts.'
    /* The secondary line is this row's only explanation, so it wraps rather
       than ellipsizes. The count is already in the heading and the primary
       line; a trailing "2 owed" said it a third time, in debt language. */
    return (
      <ul className="orb-list orb-list--rows" aria-labelledby="today-shift">
        <li>
          <button type="button" className="orb-row orb-row--link"
            onClick={() => setOutcomeOpen(true)}
            aria-label={`${primary}. Open end-of-shift outcome capture`}>
            <span className="orb-row__body">
              <span className="orb-row__primary">{primary}</span>
              <span className="orb-row__secondary orb-row__secondary--wrap">{secondary}</span>
            </span>
            <span className="orb-row__trailing">
              <span className="orb-row__go" aria-hidden="true">›</span>
            </span>
          </button>
        </li>
      </ul>
    )
  }

  return (
    <div className="orb-panel today-home animate-fade-in" ref={rootRef}>
      {/* Speaks only on an adverse increase, at most once per load. */}
      <div className="sr-only" role="status" aria-live="polite">{announcement}</div>

      <header className="orb-home__header">
        <div className="today-header__block">
          <h1 className="orb-home__title">Today</h1>
          <div className="today-context">
            <p
              className={`orb-home__context${veryStale ? ' orb-home__context--very-stale' : stale ? ' orb-home__context--stale' : ''}`}
              role={veryStale ? 'alert' : stale ? 'status' : undefined}
            >
              {wardLabel} · {dateLabel()}
              {boardClock && <> · updated <span className="orb-home__clock">{boardClock}</span></>}
              {(stale || veryStale) && ageMinutes !== null && <> · {ageMinutes} min ago</>}
            </p>
            {veryStale && (
              <button type="button" className="orb-home__refresh" onClick={() => { void refreshAll() }}>
                Retry
              </button>
            )}
          </div>
        </div>
        {/* Never the only path to fresh data — the 60s poll continues behind it. */}
        <button
          type="button"
          className="orb-home__refresh"
          onClick={() => { void refreshAll() }}
          disabled={refreshing}
          aria-busy={refreshing || undefined}
        >
          {refreshing ? 'Refreshing…' : 'Refresh'}
        </button>
      </header>

      <OfflineBanner snapshot={offline} what="ward board" onRetry={() => { void refreshAll() }} />

      <div className="today-grid">
        <div className="today-main">
          <Section>
            <SectionHeader id="today-deteriorating" label="Highest NEWS2 scores" unit={plural(deteriorating.length, 'patient')}
              count={deteriorating.length} tone="danger" />
            {deterioratingBody()}
          </Section>

          <Section>
            <SectionHeader id="today-pending" label="Pending my decision" unit={plural(pendingCount, 'item')}
              count={pendingCount} tone="warn" />
            {pendingBody()}
          </Section>
        </div>

        <div className="today-aside">
          <Section>
            {/* No tint, at any count. New results is a fact list, not an alarm:
                a status-tinted chip is reserved for a non-zero ADVERSE count
                (spec §1.7), and results coming back is not adverse. */}
            <SectionHeader id="today-results" label="New results" unit={plural(unseenTotal, 'result')}
              count={unseenTotal} />
            {resultsBody()}
          </Section>

          {/* PERMISSION-DENIED IS ABSENCE, NOT A DISABLED CONTROL. A greyed-out
              block is a promise being withheld and a 403-on-click is the defect
              this whole design family exists to kill, so an account without
              outcome.record simply does not have this section. */}
          {canRecordOutcome && (
            <Section>
              <SectionHeader id="today-shift" label="End of shift" unit={`${plural(owedShifts.length, 'shift')} to confirm`}
                count={owedShifts.length} tone="warn" />
              {shiftBody()}
            </Section>
          )}
        </div>
      </div>

      {/* Last, lowest, surface-coloured: a tool you reach for after reading,
          never before. NOT .sage-composer — that class lives in Sage.css, which
          ships inside SagePanel's lazy chunk, so on a landing screen the doctor
          reaches before ever opening Sage it resolves to nothing and the
          composer paints as a bare textarea on the page background. */}
      <form
        className="today-ask"
        aria-label="Ask Sage"
        onSubmit={e => { e.preventDefault(); submitQuestion() }}
      >
        <label className="sr-only" htmlFor="today-ask-input">Ask Sage about this ward</label>
        <textarea
          id="today-ask-input"
          ref={askRef}
          className="today-ask__input"
          rows={1}
          value={question}
          placeholder="Ask Sage about this ward…"
          onChange={e => setQuestion(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitQuestion() }
            else if (e.key === 'Escape') askRef.current?.blur()
          }}
        />
        <button type="submit" className="today-ask__send" disabled={!question.trim()}
          aria-label="Ask Sage">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
            strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M4 12h15" /><path d="M13 6l6 6-6 6" />
          </svg>
        </button>
      </form>

      {outcomeOpen && canRecordOutcome && (
        <OutcomeSheetModal
          onClose={() => { setOutcomeOpen(false); void loadShift() }}
          patients={(board?.patients ?? []).map(p => ({ patient_id: p.patient_id, name: p.name }))}
        />
      )}
    </div>
  )
}

/* ── The capture sheet's shell ────────────────────────────────────────────────
 *
 * Its own overlay classes, not Vigil's `.modal-overlay`: those rules live in
 * Vigil.css, which arrives with VigilPanel's lazy chunk. A doctor who lands on
 * Today and opens this before ever visiting the patient board would get an
 * unstyled dialog — the same hazard the shared-primitives rule exists for.
 *
 * THE BLINDING BOUNDARY TRAVELS WITH THE COMPONENT. The recorder must never see
 * scorecard numbers (server/permissions.py: `outcome.record` cannot read the
 * scorecard), so this dialog holds the capture sheet and nothing else — no
 * rates, no metrics, no adjudication queue.
 */
function OutcomeSheetModal({ onClose, patients }: {
  onClose: () => void
  patients: { patient_id: string; name: string }[]
}) {
  const ref = useFocusTrap<HTMLDivElement>(onClose)
  return createPortal(
    /* Portalled to <body> so the scrim escapes .shell__main's isolated stacking
       context and dims the whole chrome, not part of it. */
    <div className="today-sheet-overlay" onClick={onClose}>
      <div
        ref={ref}
        className="today-sheet"
        role="dialog"
        aria-modal="true"
        aria-labelledby="today-outcome-title"
        onClick={e => e.stopPropagation()}
      >
        <h2 className="today-sheet__title" id="today-outcome-title">End-of-shift outcomes</h2>
        <div className="today-sheet__body">
          <Suspense fallback={<div className="orb-panel-loading">Opening the capture sheet…</div>}>
            <OutcomeCaptureSheet patients={patients} />
          </Suspense>
        </div>
        <div className="today-sheet__actions">
          <button type="button" className="orb-home__refresh" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>,
    document.body,
  )
}
