/**
 * Ward Round — loading, empty and failed, as three obviously different things.
 *
 * A blank region on a clinical screen is a hazard, because "there is nothing to
 * report" and "we could not find out" look identical when both render as
 * nothing. On a round that difference decides whether a doctor walks away.
 *
 * So each state gets a distinct SHAPE, not just distinct words:
 *   · LOADING — grey skeleton bars, animated, aria-busy. Obviously in progress.
 *   · EMPTY   — an outlined card that says what is absent and states plainly
 *               that it is not a failure.
 *   · FAILED  — role="alert", a heavy status-danger left rule, the real error
 *               text, a Retry button, and the sentence that matters most: that
 *               a failed load is not a clinical all-clear.
 */
import type { ReactNode } from 'react'

export function RoundLoading({ label, lines = 3 }: { label: string; lines?: number }) {
  return (
    <div className="round-state round-state--loading" role="status" aria-busy="true" aria-live="polite">
      <span className="round-state__label">{label}</span>
      <div className="round-skeleton" aria-hidden="true">
        {Array.from({ length: lines }, (_, i) => (
          <span key={i} className="round-skeleton__bar" style={{ width: `${88 - i * 16}%` }} />
        ))}
      </div>
    </div>
  )
}

export function RoundEmpty({
  title,
  detail,
  icon,
  /** Overrides the "nothing failed to load" line. Pass null where the empty
   *  state is an ACHIEVEMENT (a finished round) rather than an absence of
   *  clinical data — there, the reassurance is just noise. */
  reassurance = 'Nothing failed to load: there is genuinely nothing recorded here.',
}: {
  title: string
  detail: string
  icon?: ReactNode
  reassurance?: string | null
}) {
  return (
    <div className="round-state round-state--empty">
      <span className="round-state__icon" aria-hidden="true">
        {icon ?? (
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="12" cy="12" r="9" />
            <path d="M8 12h8" />
          </svg>
        )}
      </span>
      <div className="round-state__text">
        <p className="round-state__title">{title}</p>
        <p className="round-state__detail">{detail}</p>
        {/* Said out loud. This is the sentence that stops an empty region being
            read as a clinical statement. */}
        {reassurance && <p className="round-state__reassure">{reassurance}</p>}
      </div>
    </div>
  )
}

export function RoundError({
  title,
  message,
  onRetry,
  retryLabel = 'Retry',
}: {
  title: string
  message: string
  onRetry?: () => void
  retryLabel?: string
}) {
  return (
    <div className="round-state round-state--error" role="alert">
      <span className="round-state__icon" aria-hidden="true">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
          <path d="M12 9v4" />
          <path d="M12 17h.01" />
        </svg>
      </span>
      <div className="round-state__text">
        <p className="round-state__title">{title}</p>
        <p className="round-state__detail">{message}</p>
        {/* The whole point of separating this from the empty state. */}
        <p className="round-state__warn">
          This is a display failure, not an all-clear. Do not read the missing
          information as normal. Check the chart directly.
        </p>
        {onRetry && (
          <button type="button" className="round-btn round-btn--quiet" onClick={onRetry}>
            {retryLabel}
          </button>
        )}
      </div>
    </div>
  )
}

/** A compact, persistent strip for a PARTIAL failure: the round is usable, but
 *  one source of information is missing and the doctor must know which. */
export function RoundDegraded({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="round-degraded" role="alert">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <circle cx="12" cy="12" r="9" />
        <path d="M12 8v5" />
        <path d="M12 16h.01" />
      </svg>
      <span>{message}</span>
      {onRetry && (
        <button type="button" className="round-btn round-btn--quiet round-btn--xs" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  )
}
