/**
 * Ward Round — the one-screen summary.
 *
 * Three questions, side by side, no scrolling and no tab-switching:
 * what changed overnight, what is wrong now, what has to be decided today.
 * Everything clinical on it is a server-computed field (see roundSummary.ts);
 * this file only lays it out.
 *
 * Two details here are clinical rather than cosmetic:
 *
 *   ALLERGIES ARE ALWAYS ON SCREEN, and when they are unknown the strip says
 *   UNKNOWN rather than showing nothing. An empty allergy row reads as "no
 *   allergies" to anyone about to prescribe, which is the wrong way to fail.
 *
 *   EACH COLUMN OWNS ITS OWN LOADING / EMPTY / FAILED STATE. The overnight feed
 *   is a separate request from the ward board, so it can fail on its own, and
 *   when it does that column must say so instead of rendering as "quiet night".
 */
import type { NursePatientCard } from '../../api/nurse'
import type { SageInsight, StoryEvent } from '../../api/client'
import type { IpassReasonedAssessment } from '../../api/ipass'
import ReasonedAssessment from '../handoff/ReasonedAssessment'
import { formatUTCTime } from '../../lib/datetime'
import type { RoundPatient } from './roundQueue'
import {
  admissionDay,
  changesSince,
  currentIssues,
  decisionsToday,
  drugChartShown,
  overnightWindow,
} from './roundSummary'
import { RoundDegraded, RoundEmpty, RoundError, RoundLoading } from './RoundStates'

export type LoadPhase = 'loading' | 'ready' | 'error'

interface Props {
  patient: RoundPatient
  /** Ward-board row. Null means the ward board did not carry this patient — a
   *  gap, which currentIssues() turns into a visible warning. */
  card: NursePatientCard | null
  storyPhase: LoadPhase
  story: StoryEvent[]
  storyError: string | null
  onRetryStory: () => void
  insights: SageInsight[]
  insightsFailed: boolean
  hasPlanToday: boolean
  /** Sage's four-section assessment for this patient, or null. Null is the
   *  common case and renders NOTHING — no heading, no chip, no spinner. */
  assessment: IpassReasonedAssessment | null
  now: Date
}

const SEVERITY_CLASS = { critical: 'danger', warning: 'warn', info: 'info' } as const

/** The same refusal ReasonedAssessment makes (no heading over nothing), checked
 *  one layer out so the collapsible wrapper and its summary line never render
 *  around an assessment the block itself would refuse to show. */
function hasAssessment(a: IpassReasonedAssessment | null): a is IpassReasonedAssessment {
  return !!a && !!a.heading && (a.whats_happening || '').trim() !== ''
}

function AllergyStrip({ card }: { card: NursePatientCard | null }) {
  if (!card) {
    return (
      <div className="round-allergies round-allergies--unknown" role="alert">
        <span className="round-allergies__label">Allergies</span>
        <span className="round-allergies__unknown">
          Unknown, ward data unavailable. Check the chart before prescribing.
        </span>
      </div>
    )
  }
  if (card.allergies.length === 0) {
    return (
      <div className="round-allergies">
        <span className="round-allergies__label">Allergies</span>
        <span className="round-allergies__none">None recorded</span>
      </div>
    )
  }
  const ordered = [...card.allergies].sort(
    (a, b) => (b.severity === 'severe' ? 1 : 0) - (a.severity === 'severe' ? 1 : 0),
  )
  return (
    <div className="round-allergies round-allergies--present">
      <span className="round-allergies__label">Allergies</span>
      <ul className="round-allergies__list">
        {ordered.map((a, i) => (
          <li
            key={`${a.substance}-${i}`}
            className={`round-chip round-chip--${a.severity === 'severe' ? 'danger' : 'warn'}`}
          >
            {a.substance}
            {a.reaction ? <span className="round-chip__sub"> · {a.reaction}</span> : null}
          </li>
        ))}
      </ul>
    </div>
  )
}

export default function RoundSummaryView({
  patient, card, storyPhase, story, storyError, onRetryStory,
  insights, insightsFailed, hasPlanToday, assessment, now,
}: Props) {
  const window = overnightWindow(now)
  const feed = changesSince(story, window, 5)
  const issues = currentIssues(card, insights)
  const decisions = decisionsToday(card, insights, hasPlanToday)
  const day = admissionDay(patient.admission_date, now)

  return (
    <section className="round-summary" aria-label={`Summary for ${patient.name}`}>
      <header className="round-patient">
        <span className="round-patient__bed">{patient.bed || 'No bed'}</span>
        <div className="round-patient__id">
          <h2 className="round-patient__name">{patient.name}</h2>
          <p className="round-patient__meta">
            {[
              patient.age ? `${patient.age}` : null,
              patient.sex || null,
              day ? `Day ${day}` : null,
            ].filter(Boolean).join(' · ')}
            {patient.mrn ? <span className="round-patient__mrn"> {patient.mrn}</span> : null}
          </p>
        </div>
        <p className="round-patient__dx">{patient.diagnosis || 'No diagnosis recorded'}</p>
      </header>

      <AllergyStrip card={card} />

      <div className="round-cols">
        {/* ── What changed ── */}
        <section className="round-col" aria-labelledby="round-col-changed">
          <h3 className="round-col__title" id="round-col-changed">
            Since {window.sinceLabel}
          </h3>
          {storyPhase === 'loading' ? (
            <RoundLoading label="Loading overnight events…" />
          ) : storyPhase === 'error' ? (
            <RoundError
              title="Overnight events unavailable"
              message={storyError ?? 'The request failed.'}
              onRetry={onRetryStory}
            />
          ) : feed.events.length === 0 ? (
            <RoundEmpty
              title="Nothing recorded overnight"
              detail={`No notes, doses, orders or alerts were charted since ${window.sinceLabel}.`}
            />
          ) : (
            <>
              <ul className="round-list">
                {feed.events.map((e, i) => (
                  <li key={`${e.timestamp}-${i}`} className={`round-item round-item--${e.type}`}>
                    <span className="round-item__time">{formatUTCTime(e.timestamp)}</span>
                    <span className="round-item__body">
                      <span className="round-item__title">{e.title}</span>
                      {e.detail ? <span className="round-item__detail">{e.detail}</span> : null}
                    </span>
                  </li>
                ))}
              </ul>
              {feed.hidden > 0 && (
                // Never silently truncate on a clinical screen.
                <p className="round-col__more">
                  {feed.hidden} more event{feed.hidden === 1 ? '' : 's'} overnight: open the full chart to see them.
                </p>
              )}
            </>
          )}
        </section>

        {/* ── Current issues ── */}
        <section className="round-col" aria-labelledby="round-col-issues">
          <h3 className="round-col__title" id="round-col-issues">Current issues</h3>
          {insightsFailed && (
            <RoundDegraded message="Sage surveillance flags could not be loaded. This list may be incomplete." />
          )}
          {issues.length === 0 ? (
            <RoundEmpty
              title="No active issues"
              detail={drugChartShown()
                ? 'The ward board scores this patient as stable, observations are up to date and no medication is overdue.'
                : 'Observations are up to date.'}
            />
          ) : (
            <ul className="round-list">
              {issues.map(issue => (
                <li key={issue.id} className={`round-item round-item--${SEVERITY_CLASS[issue.severity]}`}>
                  <span className={`round-dot round-dot--${SEVERITY_CLASS[issue.severity]}`} aria-hidden="true" />
                  <span className="round-item__body">
                    <span className="round-item__title">
                      {issue.label}
                      {/* The word as well as the colour. */}
                      <span className="round-item__sev"> · {issue.severity}</span>
                    </span>
                    {issue.detail ? <span className="round-item__detail">{issue.detail}</span> : null}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>

        {/* ── Decide today ── */}
        <section className="round-col" aria-labelledby="round-col-decide">
          <h3 className="round-col__title" id="round-col-decide">Decide today</h3>
          {decisions.length === 0 ? (
            <RoundEmpty
              title="Nothing outstanding"
              detail={drugChartShown()
                ? "No unverified medications, no open Sage suggestions, and today's plan is already documented."
                : "Today's plan is already documented."}
            />
          ) : (
            <ul className="round-list">
              {decisions.map(d => (
                <li key={d.id} className={`round-item round-item--decide round-item--src-${d.source}`}>
                  <span className="round-item__body">
                    <span className="round-item__title">{d.label}</span>
                    <span className="round-item__detail">{d.detail}</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>

      {/* Sage's reasoned assessment — BELOW the three columns, deliberately.
          The columns are what the round is FOR (what changed, what is wrong,
          what to decide) and every one of them is either charted fact or Orb's
          own deterministic arithmetic. A machine-authored opinion does not
          compete with those for the top of the screen; it sits under them,
          labelled, for the doctor who wants a second read. Renders nothing when
          there is no assessment.

          Collapsed by default. Rendered open it was the last flex child of a
          clipped column and squeezed the three columns to 0px, so the sickest
          patients (the ones with a supplement) lost "what changed", "current
          issues" and "decide today". The summary line carries the server's
          heading verbatim, so the labelling is on screen before it is opened.
          Keyed by assessment so an opened block never stays open for the next
          bed. */}
      {hasAssessment(assessment) && (
        <details className="round-assess" key={`${patient.id}:${assessment.assessment_id ?? ''}`}>
          <summary className="round-assess__summary">{assessment.heading}</summary>
          <ReasonedAssessment assessment={assessment} />
        </details>
      )}
    </section>
  )
}
