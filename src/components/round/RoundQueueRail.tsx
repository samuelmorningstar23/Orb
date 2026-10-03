/**
 * Ward Round — the queue rail.
 *
 * This is the "queue, not a list" made visible: the beds in walk order, what
 * has been done to each, and where the doctor is standing. It is a navigation
 * control, so it is a real <button> per bed — reachable by Tab, activated by
 * Enter or Space, and 44px tall for a gloved thumb.
 *
 * Status is carried by an ICON AND A WORD, never by colour alone: colour-blind
 * clinicians and a sunlit ward window both defeat a colour-only cue.
 */
import type { RoundPatient, RoundProgress, RoundStatus } from './roundQueue'
import { statusOf } from './roundQueue'

const STATUS_WORD: Record<RoundStatus, string> = {
  seen: 'Seen',
  skipped: 'Skipped, still owed',
  pending: 'Not yet seen',
}

function StatusMark({ status }: { status: RoundStatus }) {
  if (status === 'seen') {
    return (
      <svg className="round-rail__mark" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M20 6 9 17l-5-5" />
      </svg>
    )
  }
  if (status === 'skipped') {
    return (
      <svg className="round-rail__mark" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M4 12h11" />
        <path d="m12 8 4 4-4 4" />
        <path d="M20 5v14" />
      </svg>
    )
  }
  return (
    <svg className="round-rail__mark" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <circle cx="12" cy="12" r="6" />
    </svg>
  )
}

interface Props {
  queue: RoundPatient[]
  statuses: Record<string, RoundStatus>
  cursor: string | null
  progress: RoundProgress
  onSelect: (patientId: string) => void
}

export default function RoundQueueRail({ queue, statuses, cursor, progress, onSelect }: Props) {
  const pct = progress.total > 0 ? Math.round((progress.seen / progress.total) * 100) : 0

  return (
    <nav className="round-rail" aria-label="Round queue">
      <div className="round-rail__head">
        <div className="round-rail__count">
          <strong>{progress.seen}</strong>
          <span> of {progress.total} seen</span>
        </div>
        {/* aria-live so a keyboard/screen-reader user hears progress move
            without hunting for it after every action. */}
        <div
          className="round-rail__bar"
          role="progressbar"
          aria-valuenow={progress.seen}
          aria-valuemin={0}
          aria-valuemax={progress.total}
          aria-label={`${progress.seen} of ${progress.total} patients seen`}
        >
          <span className="round-rail__bar-fill" style={{ width: `${pct}%` }} />
        </div>
        <p className="round-rail__remaining" aria-live="polite">
          {progress.remaining === 0
            ? 'Every bed seen'
            : `${progress.remaining} left${progress.skipped ? ` · ${progress.skipped} skipped` : ''}`}
        </p>
      </div>

      <ul className="round-rail__list">
        {queue.map((p, i) => {
          const status = statusOf(statuses, p.id)
          const isCurrent = p.id === cursor
          return (
            <li key={p.id}>
              <button
                type="button"
                className={`round-rail__item round-rail__item--${status}${isCurrent ? ' round-rail__item--current' : ''}`}
                onClick={() => onSelect(p.id)}
                aria-current={isCurrent ? 'true' : undefined}
                aria-label={`Bed ${p.bed || 'not recorded'}, ${p.name}, ${STATUS_WORD[status]}. Position ${i + 1} of ${queue.length}.`}
              >
                <span className="round-rail__bed">{p.bed || '—'}</span>
                <span className="round-rail__who">
                  <span className="round-rail__name">{p.name}</span>
                  {/* The word, not just the icon. */}
                  <span className="round-rail__status">{STATUS_WORD[status]}</span>
                </span>
                <StatusMark status={status} />
              </button>
            </li>
          )
        })}
      </ul>
    </nav>
  )
}
