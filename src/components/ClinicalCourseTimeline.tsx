/**
 * Sage-deduced clinical course timeline.
 *
 * Renders the expected inpatient course (ordered care phases + expected length of
 * stay) that Sage reasons from the patient's diagnosis, comorbidities, age and sex —
 * with the patient's current phase highlighted. This replaces the old one-node-per-
 * calendar-day rail, which made a short illness look like a 40-day stay because of
 * the seeded admission date.
 */

interface Phase {
  name: string
  focus: string
  typical_days: string
}

interface Projection {
  expected_los_days?: number
  phases?: Phase[]
  discharge_criteria?: string[]
  rationale?: string
  current_phase_index?: number
  source?: string
}

export function ClinicalCourseTimeline({
  projection,
  failed = false,
  onRetry,
}: {
  projection: Projection | null
  /** The projection request came back with an error rather than not yet. */
  failed?: boolean
  onRetry?: () => void
}) {
  /* "Still loading" and "this failed" are not the same screen.
     The caller used to swallow the rejection, so a failed projection left this
     component on `projection === null` — which rendered "Sage is charting the
     expected course…" with a live pulse animation FOREVER. A clinician waiting
     for the expected length of stay had no way to learn it was never coming,
     and no way to ask again. */
  if (failed && !projection) {
    return (
      <div className="glass-card course-timeline">
        <div className="course-timeline__head">
          <h3 className="detail__section-title" style={{ margin: 0 }}>Clinical Course</h3>
        </div>
        <div className="course-timeline__loading" role="alert">
          <span>
            Clinical course unavailable: this is a display failure, not a
            statement about this patient&rsquo;s course.
          </span>
          {onRetry && (
            <button type="button" className="course-timeline__retry" onClick={onRetry}>
              Retry
            </button>
          )}
        </div>
      </div>
    )
  }

  if (!projection) {
    return (
      <div className="glass-card course-timeline">
        <div className="course-timeline__head">
          <h3 className="detail__section-title" style={{ margin: 0 }}>Clinical Course</h3>
        </div>
        <div className="course-timeline__loading" role="status">
          <span className="course-timeline__pulse" />
          Sage is charting the expected course…
        </div>
      </div>
    )
  }

  const phases = projection.phases || []
  const current = Math.min(Math.max(projection.current_phase_index ?? 0, 0), Math.max(phases.length - 1, 0))

  return (
    <div className="glass-card course-timeline">
      <div className="course-timeline__head">
        <h3 className="detail__section-title" style={{ margin: 0 }}>Clinical Course</h3>
        <div className="course-timeline__meta">
          {projection.expected_los_days != null && (
            <span className="course-timeline__los">~{projection.expected_los_days}-day expected stay</span>
          )}
          <span className={`course-timeline__source course-timeline__source--${projection.source === 'llm' ? 'sage' : 'est'}`}>
            {projection.source === 'llm' ? '✦ Sage-projected' : 'Estimated'}
          </span>
        </div>
      </div>

      <div className="course-rail">
        {phases.map((p, i) => {
          const state = i < current ? 'done' : i === current ? 'current' : 'future'
          return (
            <div key={i} className={`course-phase course-phase--${state}`}>
              <div className="course-phase__marker">{i < current ? '✓' : i + 1}</div>
              <div className="course-phase__name">
                {p.name}
                {i === current && <span className="course-phase__now">Now</span>}
              </div>
              {p.typical_days && <div className="course-phase__days">{p.typical_days}</div>}
              {p.focus && <div className="course-phase__focus">{p.focus}</div>}
            </div>
          )
        })}
      </div>

      {projection.rationale && (
        <div className="course-timeline__rationale">{projection.rationale}</div>
      )}

      {projection.discharge_criteria && projection.discharge_criteria.length > 0 && (
        <div className="course-timeline__dc">
          <span className="course-timeline__dc-label">Discharge when</span>
          <div className="course-timeline__dc-list">
            {projection.discharge_criteria.map((c, i) => (
              <span key={i} className="course-timeline__dc-item">{c}</span>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
