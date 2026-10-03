import './InvestigationalBanner.css'

/** The fixed lead line, exported so tests and other surfaces quote it exactly. */
export const INVESTIGATIONAL_LEAD = 'Investigational, not for diagnostic use.'

/**
 * The standing label on Orb's AI surfaces (Lens, Sage, the floating Sage card).
 *
 * Static on purpose. The only disclaimer Lens had sat inside the editable
 * findings box and arrived with the model's output, so it was absent before an
 * analysis, absent when the stream failed, and one select-all away from deleted.
 * This renders unconditionally: it depends on no model output, no provenance
 * fetch and nothing the clinician can edit.
 */
export default function InvestigationalBanner({
  detail,
  variant,
}: {
  /** One plain sentence on what this surface is. */
  detail?: string
  /** 'compact' for the floating card, 'sage' to sit in the Sage reading column. */
  variant?: 'compact' | 'sage'
}) {
  return (
    <p
      className={`orb-investigational${variant ? ` orb-investigational--${variant}` : ''}`}
      role="note"
    >
      <strong className="orb-investigational__lead">{INVESTIGATIONAL_LEAD}</strong>
      {detail ? <span className="orb-investigational__detail"> {detail}</span> : null}
    </p>
  )
}
