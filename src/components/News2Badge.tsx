import type { CSSProperties } from 'react'
import type { News2 } from '../api/nurse'
import { news2Description } from '../lib/news2'
import { useClinicalSwitches } from '../shellConfig'

/**
 * The NEWS2 aggregate, shown as the integer the RCP escalation table is keyed
 * on — never the 0-1 normalized index, which is not a probability and must not
 * be read as one at a bedside.
 *
 * The single-parameter-3 rule is called out explicitly: a patient can sit at an
 * aggregate of 3 and still need urgent review, and that is the case most likely
 * to be missed by someone scanning a board for big numbers. It is spelled out
 * as a visible "Red score" pill under the number. It used to be a bare "3" in
 * the corner, whose meaning lived only in the hover title, so a tablet showed
 * "13" with a red superscript 3 that read as an exponent or an unread count.
 *
 * WHY THIS LIVES IN src/components AND ITS CSS IN ClinicianShell.css
 * It used to live in src/pages/panels/nurse/ with its `.news2*` rules inside
 * NursePanel.css. NursePanel is a lazy chunk, so its stylesheet only arrives
 * once a nurse opens My Shift. The doctor home renders this same badge and a
 * doctor has no `shift` tab at all — so the badge would have painted as a bare
 * number with no box, no band and no colour, and the BAND IS THE CLINICAL
 * SIGNAL. Shared component, shared (eager) stylesheet; see
 * DUAL_EXPERIENCE_VISUAL_SPEC.md §5.1 and the header of NursePanel.css.
 *
 * ACCESSIBILITY: the meaning is carried by an `aria-label`, not only by the
 * `title`. A `title` is a hover-only affordance — invisible on the bedside
 * tablet this is mostly read on, and inconsistently announced by screen
 * readers. The title stays for pointer users (§9.4).
 */
interface Props {
  news2: News2 | null
  size?: 'sm' | 'lg'
  /** Show "NEWS2" above the number. Off in dense list rows. */
  labelled?: boolean
}

export default function News2Badge({ news2, size = 'sm', labelled = true }: Props) {
  /* Scoring switched off on this computer (ORB_SCORING_ENABLED, the Orb Ward
     Record): no badge at all. Not the "—" of a patient nobody has measured,
     which would read as a score that is missing rather than one that is off. */
  const { scoringEnabled } = useClinicalSwitches()
  if (!scoringEnabled) return null
  if (!news2) {
    const none = news2Description(null)
    return (
      /* The visible "—" announces as nothing, so the label carries the whole
         fact: not recorded is different from zero, and a reader must not be
         left to infer which one they are looking at. */
      <span className={`news2 news2--${size} news2--none`} title="No observations recorded" aria-label={none} role="img">
        {labelled && <span className="news2__label">NEWS2</span>}
        <span className="news2__value">—</span>
      </span>
    )
  }
  const single3 = news2.single_param_3
  const description = news2Description(news2)
  // A small badge sits in a 56px row lead: the pill wraps to two short lines
  // and the badge gives up some side padding so it still fits.
  const compact = size === 'sm'
  return (
    <span
      className={`news2 news2--${size} news2--${news2.level}`}
      title={description}
      aria-label={description}
      role="img"
      style={single3 && compact ? { paddingInline: 'var(--space-1)' } : undefined}
    >
      {labelled && <span className="news2__label">NEWS2</span>}
      {/* An incomplete set is a FLOOR: an unmeasured vital scores 0, so the
          true NEWS2 can only be higher. "≥" says so on the number itself; the
          label says it in words (news2Description). */}
      <span className="news2__value">
        {news2.complete === false ? `≥${news2.raw_news2}` : news2.raw_news2}
      </span>
      {/* aria-hidden: the badge's aria-label already says "a single parameter
          scores 3", and role="img" announces only that label. */}
      {single3 && (
        <span
          className="news2__red"
          aria-hidden="true"
          style={{ ...RED_SCORE_PILL, ...(compact ? { width: 'min-content' } : null) }}
        >
          Red score
        </span>
      )}
    </span>
  )
}

/* Inline so the pill ships with the component on every surface that renders
   it. A danger fill with on-fill text and an on-fill outline, so it stays a
   distinct pill even inside a critical (danger-filled) badge. */
const RED_SCORE_PILL: CSSProperties = {
  marginTop: 2,
  padding: '1px 3px',
  borderRadius: 4,
  border: '1px solid var(--status-on-fill)',
  background: 'var(--status-danger)',
  color: 'var(--status-on-fill)',
  fontSize: '0.7rem',
  fontWeight: 700,
  lineHeight: 1.1,
  textAlign: 'center',
}
