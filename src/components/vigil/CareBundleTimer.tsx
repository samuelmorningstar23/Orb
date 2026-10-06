/**
 * Orb Hospital OS — Care Bundle (Sepsis Six) Timer widget
 *
 * A compact, self-contained widget for a patient detail sidebar. Polls the
 * patient's ACTIVE care bundle (~15s) and renders a live elapsed timer that
 * ticks client-side (1s) off triggered_at, an overall compliance badge, and a
 * checklist of the six bundle elements with per-element "Mark done" actions.
 *
 * Styling is inline and uses the app's design tokens (var(--...)) with hex
 * fallbacks so it drops into the dark-glass aesthetic without a stylesheet.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  getPatientBundles,
  startBundle,
  completeBundleElement,
  cancelBundle,
  type CareBundle,
  type BundleElement,
  type BundleElementState,
} from '../../api/bundles';
import { formatUTCDateTime, formatUTCTime } from '../../lib/datetime';
import { useVisiblePolling } from '../../hooks/useVisiblePolling';

const POLL_MS = 15_000;

interface CareBundleTimerProps {
  patientId: string;
}

// ── Design tokens (with fallbacks so the widget is self-contained) ──
const GREEN = 'var(--status-ok)';
const AMBER = 'var(--status-warn)';
const RED = 'var(--status-danger)';
const RED_TEXT = 'var(--status-danger-text)';
const GREEN_TEXT = 'var(--status-ok-text)';
const AMBER_TEXT = 'var(--status-warn-text)';
const TEXT_PRIMARY = 'var(--text-primary)';
const TEXT_SECONDARY = 'var(--text-secondary)';
const TEXT_TERTIARY = 'var(--text-tertiary)';
const BORDER = 'var(--border-glass)';
const BORDER_SUBTLE = 'var(--border-subtle)';
const GLASS = 'var(--bg-glass)';

// ── Small helpers ──

/** Parse an ISO timestamp; tolerate a missing trailing zone. */
function parseTs(ts: string | null | undefined): number | null {
  if (!ts) return null;
  const withZone = /[zZ]|[+-]\d\d:?\d\d$/.test(ts) ? ts : `${ts}Z`;
  const ms = Date.parse(withZone);
  return Number.isNaN(ms) ? null : ms;
}

/** A stopwatch stops being a stopwatch after a day.
 *
 *  `227:43:31` against a 60-minute target is not a reading anybody can use —
 *  it is a number that has to be divided by 24 in the reader's head, printed
 *  in the position where a live clinical timer normally goes. Under a day this
 *  is still a second-by-second stopwatch (which is what it is FOR, in the hour
 *  when the Sepsis Six matters); beyond a day it becomes a plain age, because
 *  at that point the honest statement is "this bundle was abandoned nine days
 *  ago", not "9,466 minutes elapsed". */
function formatDuration(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const days = Math.floor(s / 86400);
  if (days >= 1) {
    const hrs = Math.floor((s % 86400) / 3600);
    return `${days}d ${hrs}h`;
  }
  const hrs = Math.floor(s / 3600);
  const mins = Math.floor((s % 3600) / 60);
  const secs = s % 60;
  const mm = String(mins).padStart(2, '0');
  const ss = String(secs).padStart(2, '0');
  return hrs > 0 ? `${hrs}:${mm}:${ss}` : `${mm}:${ss}`;
}

// `color` is the small-text token, `fill` the tint behind it. The chip text
// used the fill tokens, which measure below 4.5:1 on their own tint.
const STATE_META: Record<
  BundleElementState,
  { label: string; color: string; fill: string }
> = {
  pending: { label: 'Pending', color: TEXT_TERTIARY, fill: TEXT_TERTIARY },
  overdue: { label: 'Overdue', color: RED_TEXT, fill: RED },
  done_on_time: { label: 'On time', color: GREEN_TEXT, fill: GREEN },
  done_late: { label: 'Late', color: AMBER_TEXT, fill: AMBER },
};

/** A bundle open longer than this is not a live hour any more: it was left
 *  open, and the card says that instead of running a stopwatch. */
const STALE_AFTER_SEC = 24 * 60 * 60;

function isStale(triggeredAt: string, now: number): boolean {
  const startMs = parseTs(triggeredAt);
  return startMs != null && (now - startMs) / 1000 >= STALE_AFTER_SEC;
}

/** Who opened the bundle and when, in one line a nurse can read at a glance:
 *  "Opened by Dr Meera Sharma, 04:49 PM" for a clinician, "Opened by Orb at
 *  NEWS2 12, 04:49 PM" for an automatic start. The card used to say only
 *  "Trigger: Manual start", so nobody could tell who had started the clock. */
function openedByLine(bundle: Pick<CareBundle,
  'triggered_at' | 'source' | 'triggered_by' | 'started_by_name' | 'trigger_news2'>): string {
  const at = formatUTCTime(bundle.triggered_at);
  const when = at ? `, ${at}` : '';
  const auto = bundle.source === 'vigil_auto' || bundle.triggered_by === 'vigil_auto';
  if (auto) {
    const score = typeof bundle.trigger_news2 === 'number' ? ` at NEWS2 ${bundle.trigger_news2}` : '';
    return `Opened by Orb${score}${when}`;
  }
  return `Opened by ${bundle.started_by_name || 'a member of staff'}${when}`;
}

/** The reason is worth a line of its own only when someone typed one. The
 *  default "Manual start" and an automatic NEWS2 criterion are already said by
 *  the opened-by line. */
function typedReason(bundle: Pick<CareBundle, 'trigger_reason' | 'source' | 'triggered_by'>): string | null {
  if (bundle.source === 'vigil_auto' || bundle.triggered_by === 'vigil_auto') return null;
  const reason = (bundle.trigger_reason || '').trim();
  return reason && reason.toLowerCase() !== 'manual start' ? reason : null;
}

// ── Live ticking elapsed timer (decoupled so only this node re-renders/sec) ──

function ElapsedTimer({ triggeredAt }: { triggeredAt: string }) {
  const startMs = parseTs(triggeredAt);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const elapsedSec = startMs != null ? (now - startMs) / 1000 : 0;

  // Past a day the number stops being a stopwatch (see formatDuration) and the
  // honest statement is that the bundle was never closed, in plain text, not
  // a red clock that reads as a breach happening now.
  if (startMs != null && elapsedSec >= STALE_AFTER_SEC) {
    const days = Math.floor(elapsedSec / 86400);
    return (
      <span role="timer" aria-live="off" style={{ fontSize: '1rem', fontWeight: 600, color: TEXT_SECONDARY }}>
        Open {days} {days === 1 ? 'day' : 'days'}, not closed
      </span>
    );
  }

  // Sepsis Six target is 60 minutes; tint the clock once that is breached.
  const breached = elapsedSec > 60 * 60;

  return (
    <span
      // aria-live off: a per-second updating number would flood a screen reader.
      aria-live="off"
      role="timer"
      style={{ display: 'inline-flex', alignItems: 'baseline', gap: 6 }}
    >
      <span style={{ fontSize: '0.78rem', color: TEXT_SECONDARY }}>Elapsed</span>
      <span
        style={{
          fontFamily: 'var(--font-mono, monospace)',
          fontSize: '1.5rem',
          fontWeight: 600,
          letterSpacing: '0.02em',
          color: breached ? RED_TEXT : TEXT_PRIMARY,
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        {startMs != null ? formatDuration(elapsedSec) : '--:--'}
      </span>
      <span style={{ fontSize: '0.78rem', color: TEXT_SECONDARY }}>of 60 min</span>
    </span>
  );
}

// ── Main widget ──

export default function CareBundleTimer({ patientId }: CareBundleTimerProps) {
  const [bundle, setBundle] = useState<CareBundle | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false); // an action (start/complete/cancel) in flight
  /* Cancelling a sepsis protocol mid-hour is a clinical decision, so it asks for
     a reason (the server refuses without one and audits it with the caller's
     name). It used to be a bare window.confirm that recorded nothing. */
  const [cancelOpen, setCancelOpen] = useState(false);
  const [cancelReason, setCancelReason] = useState('');
  // When the bundle was last read: the clock the card's "left open for days"
  // check uses, so render stays pure (the poll refreshes it every 15 s).
  const [readAt, setReadAt] = useState(() => Date.now());
  const mountedRef = useRef(true);

  const refetch = useCallback(
    async (opts: { showSpinner?: boolean } = {}) => {
      if (opts.showSpinner) setLoading(true);
      try {
        const bundles = await getPatientBundles(patientId, true);
        if (!mountedRef.current) return;
        setReadAt(Date.now());
        setBundle(bundles[0] ?? null);
        setError(null);
      } catch (e) {
        if (!mountedRef.current) return;
        setError(e instanceof Error ? e.message : 'Failed to load care bundle');
      } finally {
        if (mountedRef.current && opts.showSpinner) setLoading(false);
      }
    },
    [patientId],
  );

  // Initial load, reset when the patient changes.
  useEffect(() => {
    mountedRef.current = true;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    setLoading(true);
    setBundle(null);
    setError(null);
    refetch({ showSpinner: true });
    return () => {
      mountedRef.current = false;
    };
  }, [refetch]);
  // The poll stands down while the chart's panel is hidden and refetches on
  // return, so the countdown never resumes from a stale bundle.
  useVisiblePolling(() => { void refetch(); }, POLL_MS);

  const runAction = useCallback(
    async (fn: () => Promise<CareBundle | { ok: boolean }>) => {
      setBusy(true);
      try {
        await fn();
        await refetch();
      } catch (e) {
        if (mountedRef.current) {
          setError(e instanceof Error ? e.message : 'Action failed');
        }
      } finally {
        if (mountedRef.current) setBusy(false);
      }
    },
    [refetch],
  );

  const handleStart = () =>
    runAction(() => startBundle(patientId, 'Manual start (Sepsis Six)'));
  const handleComplete = (el: BundleElement) =>
    runAction(() => completeBundleElement(bundle!.id, el.key));
  const handleCancel = () => {
    const reason = cancelReason.trim();
    if (!bundle || !reason) return;
    runAction(async () => {
      const res = await cancelBundle(bundle.id, reason);
      if (mountedRef.current) {
        setCancelOpen(false);
        setCancelReason('');
      }
      return res;
    });
  };

  // ── Render ──

  if (loading) {
    return (
      <div style={cardStyle}>
        <div style={{ color: TEXT_TERTIARY, fontSize: '0.85rem' }}>
          Loading care bundle…
        </div>
      </div>
    );
  }

  if (error && !bundle) {
    return (
      <div style={cardStyle}>
        <div style={headerRowStyle}>
          <span style={titleStyle}>Care Bundle</span>
        </div>
        <div style={{ color: RED_TEXT, fontSize: '0.82rem', marginBottom: 8 }}>
          {error}
        </div>
        <button style={secondaryBtnStyle} onClick={() => refetch({ showSpinner: true })}>
          Retry
        </button>
      </div>
    );
  }

  // Empty — no active bundle.
  if (!bundle) {
    return (
      <div style={cardStyle}>
        <div style={headerRowStyle}>
          <span style={titleStyle}>Care Bundle</span>
        </div>
        <div style={{ color: TEXT_TERTIARY, fontSize: '0.82rem', marginBottom: 10 }}>
          No active care bundle
        </div>
        <button
          style={primaryBtnStyle}
          onClick={handleStart}
          disabled={busy}
        >
          {busy ? 'Starting…' : 'Start Sepsis Six'}
        </button>
        {error && (
          <div style={{ color: RED_TEXT, fontSize: '0.75rem', marginTop: 8 }}>{error}</div>
        )}
      </div>
    );
  }

  /* ONE progress statement, the chip below. The card used to say it three
     times (a "0/6 done" header, the chip, a "0% complete" footer), and the
     chip was green "on track" at 0 of 6 because nothing was overdue yet. It is
     neutral until an element is done, green only while done elements are on
     time, amber past the target, and neutral again for a bundle left open
     for days, which is a record to close, not a breach in progress. */
  const stale = isStale(bundle.triggered_at, readAt);
  const tone: 'neutral' | 'ok' | 'warn' = stale
    ? 'neutral'
    : !bundle.on_track
      ? 'warn'
      : bundle.completed_count > 0 ? 'ok' : 'neutral';
  const chipText = tone === 'ok' ? GREEN_TEXT : tone === 'warn' ? AMBER_TEXT : TEXT_SECONDARY;
  const chipBorder = tone === 'ok' ? GREEN : tone === 'warn' ? AMBER : BORDER;
  const chipWords = stale
    ? 'not closed'
    : !bundle.on_track
      ? 'past the 60-minute target'
      : bundle.completed_count > 0 ? 'on track' : 'within the 60-minute target';

  return (
    <div style={cardStyle}>
      <div style={headerRowStyle}>
        <span style={titleStyle}>Sepsis Six</span>
      </div>

      {/* Live timer + compliance badge */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'flex-start',
          gap: 20,
          marginBottom: 8,
        }}
      >
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          <ElapsedTimer triggeredAt={bundle.triggered_at} />
          <span style={{ fontSize: '0.68rem', color: TEXT_TERTIARY }}>
            Started {formatUTCDateTime(bundle.triggered_at) || 'at the trigger'}
          </span>
        </div>
        <span
          // Compliance is the meaningful state change worth announcing.
          aria-live="polite"
          data-testid="bundle-progress"
          data-tone={tone}
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 4,
            padding: '4px 10px',
            borderRadius: 'var(--radius-full, 9999px)',
            fontSize: '0.78rem',
            fontWeight: 600,
            color: chipText,
            background: GLASS,
            border: `1px solid ${chipBorder}`,
            whiteSpace: 'nowrap',
          }}
        >
          {/* "0% at risk" was read by every reviewer as "no risk", sitting
              200px from a NEWS2 16 CRITICAL badge on the same patient. The
              percentage is BUNDLE COMPLETION, so say that: "0 of 6 done · past
              the 60-minute target" cannot be misread in either direction. */}
          {bundle.completed_count} of {bundle.total_count} done · {chipWords}
        </span>
      </div>

      <div
        data-testid="bundle-opened-by"
        style={{
          fontSize: '0.74rem',
          color: TEXT_SECONDARY,
          marginBottom: 10,
          paddingBottom: 8,
          borderBottom: `1px solid ${BORDER_SUBTLE}`,
        }}
      >
        {openedByLine(bundle)}
        {typedReason(bundle) && <> · Reason: {typedReason(bundle)}</>}
      </div>

      {/* Element checklist — flows into columns on wide detail panels so the
          card fills the row instead of leaving a tall, narrow void. */}
      <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: '8px 28px' }}>
        {bundle.elements.map((el) => {
          const meta = STATE_META[el.state];
          const done = el.state === 'done_on_time' || el.state === 'done_late';
          return (
            <li
              key={el.key}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                fontSize: '0.8rem',
              }}
            >
              <span
                aria-hidden
                style={{
                  width: 16,
                  textAlign: 'center',
                  color: done ? meta.color : TEXT_TERTIARY,
                  flexShrink: 0,
                }}
              >
                {done ? '✓' : '○'}
              </span>
              <span style={{ flex: 1, minWidth: 0 }}>
                <span
                  style={{
                    color: done ? TEXT_SECONDARY : TEXT_PRIMARY,
                    textDecoration: done ? 'line-through' : 'none',
                  }}
                >
                  {el.label}
                </span>
                <span style={{ color: TEXT_TERTIARY, fontSize: '0.68rem', marginLeft: 6 }}>
                  within {el.target_min}m
                </span>
              </span>
              <span
                style={{
                  fontSize: '0.66rem',
                  fontWeight: 600,
                  color: meta.color,
                  padding: '2px 6px',
                  borderRadius: 'var(--radius-full, 9999px)',
                  background: `color-mix(in srgb, ${meta.fill} 10%, transparent)`,
                  whiteSpace: 'nowrap',
                  flexShrink: 0,
                }}
              >
                {meta.label}
              </span>
              {!done && (
                <button
                  style={markDoneBtnStyle}
                  onClick={() => handleComplete(el)}
                  disabled={busy}
                  aria-label={`Mark ${el.label} done`}
                >
                  Mark done
                </button>
              )}
            </li>
          );
        })}
      </ul>

      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginTop: 10,
          paddingTop: 8,
          borderTop: `1px solid ${BORDER_SUBTLE}`,
        }}
      >
        {error ? (
          <span style={{ color: RED_TEXT, fontSize: '0.7rem' }}>{error}</span>
        ) : (
          <span />
        )}
        {!cancelOpen && (
          <button
            type="button"
            style={{ ...linkBtnStyle, color: RED_TEXT }}
            onClick={() => setCancelOpen(true)}
            disabled={busy}
          >
            Cancel bundle
          </button>
        )}
      </div>

      {cancelOpen && (
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            gap: 6,
            marginTop: 8,
            paddingTop: 8,
            borderTop: `1px solid ${BORDER_SUBTLE}`,
          }}
        >
          <label
            htmlFor={`bundle-cancel-reason-${bundle.id}`}
            style={{ fontSize: '0.74rem', color: TEXT_SECONDARY }}
          >
            Reason for cancelling (recorded with your name)
          </label>
          <textarea
            id={`bundle-cancel-reason-${bundle.id}`}
            value={cancelReason}
            onChange={(e) => setCancelReason(e.target.value)}
            maxLength={500}
            rows={2}
            placeholder="For example: sepsis excluded, urinary retention"
            style={reasonInputStyle}
          />
          <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', flexWrap: 'wrap' }}>
            <button
              type="button"
              style={secondaryBtnStyle}
              onClick={() => { setCancelOpen(false); setCancelReason(''); }}
              disabled={busy}
            >
              Keep bundle
            </button>
            <button
              type="button"
              style={!cancelReason.trim() || busy ? disabledBtnStyle : dangerBtnStyle}
              onClick={handleCancel}
              disabled={!cancelReason.trim() || busy}
            >
              {busy ? 'Cancelling…' : 'Cancel bundle'}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Inline style objects ──

const cardStyle: React.CSSProperties = {
  background: GLASS,
  border: `1px solid ${BORDER}`,
  borderRadius: 'var(--radius-md, 16px)',
  padding: '14px 20px',
  color: TEXT_PRIMARY,
  fontFamily: 'var(--font-family, system-ui, sans-serif)',
  marginTop: 8,
};

const headerRowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  marginBottom: 8,
};

const titleStyle: React.CSSProperties = {
  fontSize: '0.82rem',
  fontWeight: 700,
  letterSpacing: '0.04em',
  textTransform: 'uppercase',
  color: TEXT_SECONDARY,
};

const primaryBtnStyle: React.CSSProperties = {
  width: '100%',
  padding: '8px 12px',
  borderRadius: 'var(--radius-sm, 12px)',
  border: 'none',
  background: 'var(--accent-amber)',
  color: 'var(--btn-primary-text)',
  fontWeight: 600,
  fontSize: '0.82rem',
  cursor: 'pointer',
};

const secondaryBtnStyle: React.CSSProperties = {
  padding: '6px 12px',
  borderRadius: 'var(--radius-sm, 12px)',
  border: `1px solid ${BORDER}`,
  background: 'transparent',
  color: TEXT_PRIMARY,
  fontSize: '0.78rem',
  cursor: 'pointer',
};

const markDoneBtnStyle: React.CSSProperties = {
  padding: '3px 8px',
  borderRadius: 'var(--radius-sm, 12px)',
  border: `1px solid ${BORDER}`,
  background: 'transparent',
  color: TEXT_SECONDARY,
  fontSize: '0.68rem',
  fontWeight: 600,
  cursor: 'pointer',
  flexShrink: 0,
};

const linkBtnStyle: React.CSSProperties = {
  border: 'none',
  background: 'transparent',
  fontSize: '0.72rem',
  fontWeight: 600,
  cursor: 'pointer',
  padding: 0,
  minHeight: 24,
};

const reasonInputStyle: React.CSSProperties = {
  width: '100%',
  boxSizing: 'border-box',
  padding: '6px 8px',
  borderRadius: 'var(--radius-sm, 12px)',
  border: `1px solid ${BORDER}`,
  background: 'transparent',
  color: TEXT_PRIMARY,
  fontFamily: 'inherit',
  fontSize: '0.78rem',
  resize: 'vertical',
};

const dangerBtnStyle: React.CSSProperties = {
  padding: '6px 12px',
  minHeight: 24,
  borderRadius: 'var(--radius-sm, 12px)',
  border: `1px solid ${RED}`,
  background: RED,
  color: 'var(--status-on-fill)',
  fontSize: '0.78rem',
  fontWeight: 600,
  cursor: 'pointer',
};

const disabledBtnStyle: React.CSSProperties = {
  ...dangerBtnStyle,
  background: 'var(--btn-disabled-bg)',
  color: 'var(--btn-disabled-text)',
  border: '1px solid var(--btn-disabled-border)',
  cursor: 'not-allowed',
};
