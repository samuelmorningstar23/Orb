/**
 * Ward Round — deriving the one-screen summary.
 *
 * The doctor has about 90 seconds at the bed and this screen does not scroll,
 * so every line on it is competing for space. This module decides what earns a
 * line, and nothing else in the round does.
 *
 * TWO RULES GOVERN EVERYTHING HERE.
 *
 * 1. NO INVENTED CLINICAL SCORING. Every clinical judgement on this screen is a
 *    field the SERVER computed — NEWS2 and its drivers, the observation
 *    schedule, medication due/overdue, pharmacist verification, Sage insights.
 *    This module classifies and orders them; it never scores a patient itself.
 *    A second, subtly different NEWS2 implementation living in the frontend is
 *    exactly the kind of thing that disagrees with the nurse's screen at 3am.
 *
 * 2. ABSENCE OF DATA IS A FINDING, NOT A CLEAN BILL. A patient with no
 *    observations recorded produces a visible warning, never an empty issues
 *    list — because an empty list on a clinical screen reads as "nothing
 *    wrong". The product has already been bitten by this once (see
 *    components/errorStates.test.tsx) and the blast radius is larger here.
 */
import type { NursePatientCard } from '../../api/nurse'
import type { SageInsight, StoryEvent } from '../../api/client'
import { parseUTC, formatClinicalTime, formatUTCDateTime } from '../../lib/datetime'
import { humaniseEnum } from '../../lib/humanise'
import { datedDetail, insightValuesAt, type DatedInsight } from '../sage/insightTime'
import { currentClinicalSwitches } from '../../shellConfig'

/** False on an Orb Ward Record computer, whose drug chart stays on paper: the
 *  round names no medicine, counts no dose and says nothing about medicines,
 *  not even that none is overdue. Read at call time. */
export function drugChartShown(): boolean {
  return !currentClinicalSwitches().wardRecord
}

/** The story types that are the drug chart (left out under the profile). */
const DRUG_CHART_STORY_TYPES = new Set<string>(['medication', 'administration'])

/** Hour of the evening handover. Everything after it is "overnight". */
const HANDOVER_HOUR = 20

/** Insight states that are finished with — they must not re-appear on a round. */
const CLOSED_INSIGHT_STATES = new Set(['dismissed', 'acted', 'resolved', 'superseded'])

/** Story event types that count as a documented clinical plan. */
const DOCUMENTATION_TYPES = new Set<StoryEvent['type']>(['note', 'scribe'])

export interface OvernightWindow {
  start: Date
  /** Human phrasing of the boundary, e.g. "08:00 PM yesterday". Shown verbatim so
   *  the doctor knows exactly what period the feed covers rather than trusting
   *  a vague "recent". */
  sinceLabel: string
}

export interface ChangeFeed {
  events: StoryEvent[]
  /** In-window events NOT shown because the screen does not scroll. Displayed,
   *  never silently swallowed. */
  hidden: number
}

export type IssueSeverity = 'critical' | 'warning' | 'info'

export interface RoundIssue {
  id: string
  severity: IssueSeverity
  label: string
  detail: string
  source: 'news2' | 'observation' | 'medication' | 'sage'
}

export interface RoundDecision {
  id: string
  label: string
  detail: string
  source: 'medication' | 'sage' | 'documentation'
}

const SEVERITY_RANK: Record<IssueSeverity, number> = { critical: 0, warning: 1, info: 2 }

function startOfLocalDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate())
}

function sameLocalDay(a: Date, b: Date): boolean {
  return startOfLocalDay(a).getTime() === startOfLocalDay(b).getTime()
}

/**
 * The period the "what changed" feed covers: since the last evening handover.
 *
 * Local wall-clock, not UTC — the ward's clock is the one the handover happened
 * on, and in IST a UTC-derived boundary lands at 01:30, mid-round.
 */
export function overnightWindow(now: Date = new Date()): OvernightWindow {
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate(), HANDOVER_HOUR, 0, 0, 0)
  if (now < start) start.setDate(start.getDate() - 1)
  // The same 12-hour clock as the rows under it ("08:00 PM"); a 24-hour
  // "20:00" header sat over "06:40 AM" entries.
  return {
    start,
    sinceLabel: `${formatClinicalTime(start)} ${sameLocalDay(start, now) ? 'today' : 'yesterday'}`,
  }
}

/**
 * In-window clinical events, newest first, capped for a screen that does not
 * scroll. `hidden` is the count the cap removed — the UI must show it, because
 * "3 more overnight" and "nothing else happened" are different clinical facts.
 */
export function changesSince(
  story: StoryEvent[],
  window: OvernightWindow,
  limit = 6,
): ChangeFeed {
  const drugChart = drugChartShown()
  const inWindow = story
    .filter(e => {
      if (!drugChart && DRUG_CHART_STORY_TYPES.has(e.type)) return false
      if (!e.timestamp) return false // Undated: never assume it was recent.
      const t = parseUTC(e.timestamp)
      return !isNaN(t.getTime()) && t >= window.start
    })
    .sort((a, b) => parseUTC(b.timestamp!).getTime() - parseUTC(a.timestamp!).getTime())

  return {
    events: inWindow.slice(0, limit),
    hidden: Math.max(0, inWindow.length - limit),
  }
}

/** Open insights that belong to THIS patient. */
function openInsightsFor(patientId: string | null, insights: SageInsight[]): SageInsight[] {
  if (!patientId) return []
  return insights.filter(
    i => i.patient_id === patientId && !CLOSED_INSIGHT_STATES.has(i.state),
  )
}

/** Insight types read off the vital signs. Antibiotic stewardship is not. */
const VITALS_INSIGHT_TYPES = new Set(['deterioration', 'trajectory', 'sepsis'])

/**
 * A vitals insight whose numbers are older than the round's latest observation.
 * The server refreshes an insight each sweep its rule still fires, so one left
 * behind by a newer set is one that set no longer supports: printing it put
 * "SpO2 falling to 91%" beside a chart reading 97.
 */
function overtakenByLatestObs(i: DatedInsight, lastObsAt: string | null): boolean {
  if (!lastObsAt || !VITALS_INSIGHT_TYPES.has(i.insight_type)) return false
  const valuesAt = parseUTC(insightValuesAt(i)).getTime()
  const obsAt = parseUTC(lastObsAt).getTime()
  return !isNaN(valuesAt) && !isNaN(obsAt) && valuesAt < obsAt
}

/**
 * Everything wrong with this patient right now, worst first.
 *
 * Returns an empty list ONLY when the server has actually scored the patient
 * and found nothing. Missing observations, an overdue set, overdue medication
 * and open Sage insights all earn a line.
 */
export function currentIssues(
  card: NursePatientCard | null,
  insights: SageInsight[],
): RoundIssue[] {
  const issues: RoundIssue[] = []

  if (!card) {
    // The ward board did not carry this patient. That is a gap in what Orb can
    // tell the doctor, and it must look like one.
    return [{
      id: 'no-card',
      severity: 'warning',
      label: 'No ward data for this patient',
      detail: drugChartShown()
        ? 'Observations, medications and scores could not be matched to this bed. Check the chart directly.'
        : 'Observations could not be matched to this bed. Check the chart directly.',
      source: 'observation',
    }]
  }

  const n = card.news2
  // Scoring switched off on this computer (the Orb Ward Record): the board
  // sends no band at all (obs_band null), so no NEWS2 line is drawn and a
  // missing score is not a finding. Only a patient with no set at all is.
  const scoringOff = card.obs_band == null
  if (scoringOff) {
    if (!card.last_obs_at) {
      issues.push({
        id: 'no-obs',
        severity: 'warning',
        label: 'No observations recorded',
        detail: 'This patient has no observation set on the record. That is missing data, not a normal result.',
        source: 'observation',
      })
    }
  } else if (!n) {
    issues.push({
      id: 'no-obs',
      severity: 'warning',
      label: 'No observations recorded',
      detail: 'This patient has no scored observation set. That is missing data, not a normal result. It does not mean the patient is stable.',
      source: 'observation',
    })
  } else {
    // Severity comes from the SERVER's band. The single-parameter-3 rule can
    // only raise it, never lower it: an aggregate of 3 driven entirely by one
    // parameter needs urgent review and is the case most easily missed by
    // someone scanning a board for big numbers.
    let severity: IssueSeverity | null =
      n.level === 'critical' ? 'critical' : n.level === 'elevated' ? 'warning' : null
    if (n.single_param_3 && severity !== 'critical') severity = 'warning'

    if (severity) {
      const parts = [...(n.drivers ?? [])]
      if (n.single_param_3) parts.push('a single parameter scores 3')
      if (n.spo2_scale === 2) parts.push('SpO₂ Scale 2')
      if (card.obs_response) parts.push(card.obs_response)
      issues.push({
        id: 'news2',
        severity,
        label: `NEWS2 ${n.raw_news2}`,
        detail: parts.join(' · '),
        source: 'news2',
      })
    }
  }

  if (card.obs_overdue) {
    issues.push({
      id: 'obs-overdue',
      severity: 'warning',
      label: 'Observations overdue',
      detail: card.last_obs_at
        // formatUTCDateTime, not the raw column: this printed
        // `2026-08-01T19:07:35.972867+00:00` to a doctor mid-round.
        ? `Last set at ${formatUTCDateTime(card.last_obs_at) || card.last_obs_at} · due every ${card.obs_interval_minutes} min`
        : `Due every ${card.obs_interval_minutes} min`,
      source: 'observation',
    })
  }

  if (drugChartShown() && (card.meds_overdue ?? 0) > 0) {
    issues.push({
      id: 'meds-overdue',
      severity: 'warning',
      label: `${card.meds_overdue} medication${card.meds_overdue === 1 ? '' : 's'} overdue`,
      detail: card.medications
        .filter(m => m.overdue)
        .map(m => m.drug_name)
        .join(', '),
      source: 'medication',
    })
  }

  for (const i of openInsightsFor(card.patient_id, insights)) {
    if (overtakenByLatestObs(i, card.last_obs_at)) continue
    issues.push({
      id: `sage-${i.id}`,
      severity: i.severity,
      label: i.title,
      // Dated: an undated detail read as the patient now.
      detail: datedDetail(i),
      source: 'sage',
    })
  }

  return issues.sort((a, b) => SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity])
}

/**
 * What this round has to decide. Deliberately short and deliberately concrete —
 * a list of "consider reviewing" items is noise at a bedside.
 *
 * `hasPlanToday` comes from documentedToday(); it is passed in rather than
 * derived here so the caller can also feed in a plan saved seconds ago in this
 * very round, before the story feed has caught up.
 */
export function decisionsToday(
  card: NursePatientCard | null,
  insights: SageInsight[],
  hasPlanToday: boolean,
): RoundDecision[] {
  const decisions: RoundDecision[] = []

  for (const m of drugChartShown() ? card?.medications ?? [] : []) {
    if (!m.unverified) continue
    decisions.push({
      id: `med-${m.medication_id}`,
      label: [m.drug_name, m.dosage, m.route].filter(Boolean).join(' '),
      detail: 'AI-extracted and not yet verified by a pharmacist. It cannot be given until it is.',
      source: 'medication',
    })
  }

  for (const i of openInsightsFor(card?.patient_id ?? null, insights)) {
    if (!i.suggested_action_type) continue
    if (overtakenByLatestObs(i, card?.last_obs_at ?? null)) continue
    const target = i.suggested_action_target ? ` ${i.suggested_action_target}` : ''
    const detail = datedDetail(i)
    decisions.push({
      id: `sage-${i.id}`,
      label: i.title,
      // Provenance travels with the suggestion. This is Sage proposing, not
      // Orb instructing, and the doctor must be able to see that at a glance.
      // The action type is a server code ("review"); it is never printed raw.
      detail: `Sage suggests: ${humaniseEnum(i.suggested_action_type)}${target}${detail ? ` · ${detail}` : ''}`,
      source: 'sage',
    })
  }

  if (!hasPlanToday) {
    decisions.push({
      id: 'no-plan',
      label: 'No plan documented today yet',
      detail: 'Dictate the plan below: this round is the record.',
      source: 'documentation',
    })
  }

  return decisions
}

/** Has a clinical note or scribe note been written for this patient today?
 *  Doses given and orders placed do NOT count — treating a patient is not the
 *  same as reviewing one, and conflating them silences the prompt on exactly
 *  the patients who are being managed but not seen. */
export function documentedToday(story: StoryEvent[], now: Date = new Date()): boolean {
  return story.some(e => {
    if (!DOCUMENTATION_TYPES.has(e.type) || !e.timestamp) return false
    const t = parseUTC(e.timestamp)
    return !isNaN(t.getTime()) && sameLocalDay(t, now)
  })
}

/** Ward day number, counted the way a whiteboard counts it: the admission date
 *  is day 1, and the count turns over at midnight rather than at the 24-hour
 *  mark. Null when no admission date is recorded. */
export function admissionDay(
  admissionDate: string | null | undefined,
  now: Date = new Date(),
): number | null {
  if (!admissionDate) return null
  const admitted = parseUTC(admissionDate)
  if (isNaN(admitted.getTime())) return null
  const days = Math.round(
    (startOfLocalDay(now).getTime() - startOfLocalDay(admitted).getTime()) / 86_400_000,
  )
  return days >= 0 ? days + 1 : null
}
