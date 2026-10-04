import { useState } from 'react'
import { resolveRoomFit, type RoomFitFlag } from '../../api/client'

/**
 * WRONG-ROOM WARNING — the strip that appears under a message the detector
 * thinks belongs to a different patient.
 *
 * WARN, NEVER BLOCK. By the time this renders the message is already sent,
 * delivered and durable. There is no "cancel" here and there deliberately never
 * will be: the interruption is for attention, not for permission.
 *
 * It is attached to the MESSAGE, not shown as a toast, because the person most
 * harmed by wrong-chart documentation is whoever reads this thread next week.
 * The resolution stays visible for the same reason — "flagged, and the nurse
 * confirmed it was correct" is information that reader needs.
 *
 * Two actions, and the safe one is not the loud one:
 *   "It's the right patient" — the override. One tap, no dialog, because a
 *   nurse who already knows the answer must not be made to argue with a
 *   heuristic. Audited server-side as a clinical event.
 *
 *   "Move to <room>" — offered ONLY when the server named a destination it is
 *   sure of and the sender may post in. An ambiguous match shows no button,
 *   because a confidently wrong destination turns one wrong-chart entry into two.
 */
export default function RoomFitWarning({
  flag,
  onResolved,
}: {
  flag: RoomFitFlag
  onResolved: (updated: RoomFitFlag) => void
}) {
  const [busy, setBusy] = useState<null | 'confirmed_correct' | 'redirected'>(null)
  const [error, setError] = useState<string | null>(null)

  const flagId = flag.flag_id ?? flag.id
  const resolved = flag.resolution && flag.resolution !== 'open'

  const act = async (decision: 'confirmed_correct' | 'redirected') => {
    if (busy || !flagId) return
    setBusy(decision)
    setError(null)
    try {
      const out = await resolveRoomFit(flagId, decision)
      onResolved(out.flag)
    } catch (err) {
      setError((err instanceof Error ? err.message : '') || 'Could not record that. Please try again.')
    } finally {
      setBusy(null)
    }
  }

  // ── Resolved: a quiet, permanent record. No buttons, no colour shouting. ──
  if (resolved) {
    const label =
      flag.resolution === 'confirmed_correct'
        ? `Checked, confirmed as the right patient${flag.resolved_by_name ? ` by ${flag.resolved_by_name}` : ''}`
        : flag.resolution === 'redirected'
          ? `Moved to ${flag.suggested_channel_name || 'another room'}${flag.resolved_by_name ? ` by ${flag.resolved_by_name}` : ''}`
          : 'Warning withdrawn automatically'
    return (
      <div className="room-fit room-fit--resolved" role="note">
        <span className="room-fit__icon" aria-hidden="true">✓</span>
        <span className="room-fit__resolved-text">{label}</span>
      </div>
    )
  }

  const who = flag.suspected_patient_name
  const headline = who
    ? `This looks like it belongs to ${who}`
    : 'This may belong to a different patient'

  return (
    <div className="room-fit" role="alert" aria-live="polite">
      <div className="room-fit__head">
        <span className="room-fit__icon" aria-hidden="true">⚠</span>
        <span className="room-fit__headline">{headline}</span>
      </div>

      <p className="room-fit__body">
        {flag.matched_surface ? (
          <>
            The message names <strong>{flag.matched_surface}</strong> and records clinical
            detail (a measurement, dose or result) about them.{' '}
          </>
        ) : (
          <>The message records clinical detail about another patient. </>
        )}
        {flag.ambiguous && !who && (
          <>More than one patient could be meant, so no destination is suggested. </>
        )}
        It has already been sent; nothing is blocked.
      </p>

      <div className="room-fit__actions">
        <button
          type="button"
          className="room-fit__btn room-fit__btn--primary"
          onClick={() => act('confirmed_correct')}
          disabled={busy !== null}
        >
          {busy === 'confirmed_correct' ? 'Recording…' : "It's the right patient"}
        </button>

        {flag.suggested_channel_id && (
          <button
            type="button"
            className="room-fit__btn"
            onClick={() => act('redirected')}
            disabled={busy !== null}
            title="Posts a copy into that room. This message stays here, marked as moved."
          >
            {busy === 'redirected'
              ? 'Moving…'
              : `Move to ${flag.suggested_channel_name || who}`}
          </button>
        )}
      </div>

      {error && <div className="room-fit__error">{error}</div>}
    </div>
  )
}
