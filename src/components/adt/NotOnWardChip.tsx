/**
 * 'Not on ward since 02:05 PM (on leave)' (adt-10).
 *
 * Shown beside a patient on the Vigil board and the nurse board when the
 * server says the patient is on leave or absconded. The row carries
 * presence_status and presence_since only while Beds and admissions (ORB_ADT)
 * is on, so with the module off this draws nothing and the boards look as
 * they always did.
 *
 * DISPLAY ONLY. It says where the patient is; it does not pause, hide or
 * change any observation or medication due time, score or alert (adt-11).
 * The time is local, through src/lib/datetime.
 */
import { notOnWardWords, presenceOf } from '../../api/adt/presence'
import './NotOnWard.css'

interface NotOnWardChipProps {
  /** Any board row: a Vigil row, a nurse card or a bed occupant. */
  row: unknown
  className?: string
}

export default function NotOnWardChip({ row, className }: NotOnWardChipProps) {
  const presence = presenceOf(row)
  const words = notOnWardWords(row)
  if (!presence || !words) return null
  return (
    <span
      className={`adt-not-on-ward adt-not-on-ward--${presence.status === 'absconded' ? 'absconded' : 'leave'}${className ? ` ${className}` : ''}`}
      data-testid="not-on-ward"
    >
      {words}
    </span>
  )
}
