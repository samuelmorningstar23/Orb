import type { SageAssessment, SageAssessmentPatientFlag } from '../../api/sageAssessment'

// The pure rules behind SageAlertAssessment: which advice lines are generic,
// what the patient-specific tier is saying, and which assessment row is the
// current one. Kept out of the component file so it exports only components
// (react-refresh/only-export-components), and so each rule stays testable
// without a screen.

/** ═══ NEVER RESTATE THE ALERT ═══
 *
 *  Sentences that are true of ANY patient with those numbers. Two of the three
 *  steps the old advice tier put on screen were these — "Escalate immediately",
 *  "Critical NEWS2" — sitting under a block that had already said the score, the
 *  drivers and the trend, deterministically, in 33-240 ms. A tier that repeats
 *  the alert is not advice; it is the reason the last one was called decoration.
 *
 *  This is the SURFACE's backstop, not the only filter: the server drops these
 *  in the pipeline where it can also count them. Two filters is deliberate — the
 *  same defence-in-depth as the drug-safety gate — and the cost of the second
 *  one firing is one withheld line, which is stated on screen rather than
 *  swallowed.
 *
 *  EACH PATTERN IS ANCHORED TO THE WHOLE LINE, and that is the whole design.
 *  A caution is generic when the generic phrase IS the line. "Escalate
 *  immediately" is banned; "Escalate early: on prednisolone 30 mg, a normal
 *  temperature does not exclude sepsis" names a chart fact and must survive,
 *  because that sentence is the entire point of the tier. A substring match
 *  would delete exactly the lines worth keeping.
 */
const GENERIC_ADVICE: readonly RegExp[] = [
  // "Escalate immediately" · "Escalate now" · "Escalate to the on-call registrar"
  /^escalate(\s+(immediately|now|urgently|care|(to|for)\s+[^.]*))?[.!]?$/i,
  // "Repeat observations" · "Repeat observations in 15 minutes" · "Recheck vitals"
  /^(repeat|recheck|re-check)\s+(the\s+)?(observations?|obs|vitals?|vital\s+signs?|news2)[^.]*[.!]?$/i,
  // "Assess response to initial interventions"
  /^(assess|review|evaluate)\s+(the\s+)?response\s+to[^.]*[.!]?$/i,
  // "Consider sepsis" · "Rule out sepsis"
  /^(consider|rule\s+out|think\s+about|exclude)\s+(sepsis|infection|deterioration)[^.]*[.!]?$/i,
  // "Monitor the patient closely" · "Observe closely"
  /^(monitor|observe|watch)\s+(the\s+)?(patient\s+)?(closely|continuously|carefully)?[^.]*[.!]?$/i,
  // "Critical NEWS2" · "High NEWS2 score"
  /^(critical|high|medium|low)\s+news2[^.]*[.!]?$/i,
  // "Review the patient" · "Review the patient urgently"
  /^review\s+(the\s+)?patient[^.]*[.!]?$/i,
  // "Commence oxygen" · "Give high-flow oxygen"
  /^(commence|start|give|administer|titrate)\s+(high[-\s]?flow\s+)?(supplemental\s+)?oxygen[^.]*[.!]?$/i,
  // "Ensure IV access" · "Obtain IV access"
  /^(ensure|obtain|secure|establish)\s+(iv|intravenous)\s+access[^.]*[.!]?$/i,
  // "Senior review" · "Urgent senior review required"
  /^(urgent\s+)?senior\s+review[^.]*[.!]?$/i,
]

/** True when this line would be true of any patient with these numbers.
 *
 *  Pure and exported so the rule is testable on its own — a filter whose only
 *  proof is a rendered screen is a filter that silently stops filtering. */
export function isGenericAdviceLine(text: string | null | undefined): boolean {
  const line = (text || '').trim().replace(/\s+/g, ' ')
  if (!line) return false
  return GENERIC_ADVICE.some(re => re.test(line))
}

/** WHAT THE ADVICE TIER IS SAYING, AS A VIEW.
 *
 *  Four outcomes, and the distinction between two of them is the one this whole
 *  change turns on:
 *
 *    - `absent`     — no block on the payload. A row written before this tier
 *                     existed. Render NOTHING; an absent block is not a finding.
 *    - `silent`     — Orb checked this chart and there is nothing to flag. A
 *                     FIRST-CLASS, EXPECTED, COMMON answer, and the reason most
 *                     alerts never reach the GPU at all.
 *    - `flags`      — a short list of checkable cautions.
 *    - `unchecked`  — the check did not produce a usable answer. The OPPOSITE of
 *                     `silent`, and never rendered as it. Two reasons, kept
 *                     apart because they are different failures: `failed` (the
 *                     server said so) and `withheld` (the server sent lines and
 *                     every one of them was dropped here — no chart fact, or a
 *                     restatement of the alert).
 *
 *  AN UNRECOGNISED `outcome` FALLS TO `unchecked`, DELIBERATELY. Claiming "no
 *  patient-specific factors identified" off a value we do not understand would
 *  be asserting a negative clinical finding Orb never made, which is exactly the
 *  collapse the pilot instrument is built to prevent.
 *
 *  Pure, so the rule is testable without a screen and cannot capture a stale
 *  closure.
 */
export type PatientSpecificView =
  | { kind: 'absent' }
  | { kind: 'silent'; modelCalled: boolean }
  | { kind: 'flags'; flags: SageAssessmentPatientFlag[]; withheldGeneric: number; withheldNoFact: number }
  | {
    kind: 'unchecked'
    reason: 'failed' | 'withheld'
    message: string | null
    withheldGeneric: number
    withheldNoFact: number
  }

export function patientSpecificView(assessment: SageAssessment | null | undefined): PatientSpecificView {
  const block = assessment?.patient_specific
  if (!block) return { kind: 'absent' }

  const raw = Array.isArray(block.flags) ? block.flags : []
  // EVERY FLAG NAMES ITS SOURCE FACT. A caution with no chart fact behind it is
  // not checkable in one glance, which is the only property that makes this tier
  // worth a clinician's attention at 3am — so it is dropped here rather than
  // rendered as a confident claim nobody can verify.
  const noFact = raw.filter(f => !(f?.fact || '').trim() || !(f?.caution || '').trim())
  const sourced = raw.filter(f => !noFact.includes(f))
  const generic = sourced.filter(f => isGenericAdviceLine(f.caution))
  const kept = sourced.filter(f => !generic.includes(f))

  // Surviving flags win over the outcome word. If the server labelled the row
  // `no_material` but sent a caution with a chart fact behind it, the honest
  // reading is that there IS something to say — dropping it to match a label
  // would hide clinical content behind a bookkeeping field.
  if (kept.length > 0) {
    return {
      kind: 'flags', flags: kept, withheldGeneric: generic.length, withheldNoFact: noFact.length,
    }
  }
  if (raw.length > 0) {
    return {
      kind: 'unchecked',
      reason: 'withheld',
      message: null,
      withheldGeneric: generic.length,
      withheldNoFact: noFact.length,
    }
  }
  if (block.outcome === 'no_candidates' || block.outcome === 'no_material') {
    return { kind: 'silent', modelCalled: Boolean(block.model_called) }
  }
  return {
    kind: 'unchecked',
    reason: 'failed',
    message: block.message || null,
    withheldGeneric: 0,
    withheldNoFact: 0,
  }
}

/** WHICH ROW IS "THE" ASSESSMENT, AND WHICH ROWS MUST NOT DISAPPEAR.
 *
 *  One patient, one case room, one row per critical alert. `rows` arrives
 *  newest-first from the server, which owns the ordering because that is where
 *  the timestamps are canonical.
 *
 *    - `current` is the newest row. It governs the state chip and the state
 *      block, and that is honest even when it failed: the LATEST alert really
 *      does have no assessment.
 *    - `carried` is every OLDER row still sitting at `ready` — written, complete
 *      and never approved or declined by anybody. A failed newer sibling is not
 *      a verdict on them, and it must not be allowed to act as one. Only `ready`
 *      qualifies: `generating` is not finished, and approved/declined are done.
 *
 *  Pure, so the rule is testable on its own and cannot capture a stale closure.
 */
export function partitionAssessments(rows: SageAssessment[]): {
  current: SageAssessment | null
  carried: SageAssessment[]
} {
  const current = rows.length > 0 ? rows[0] : null
  return { current, carried: rows.slice(1).filter(r => r.state === 'ready') }
}
